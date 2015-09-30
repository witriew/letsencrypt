"""Let's Encrypt CLI."""
# TODO: Sanity check all input.  Be sure to avoid shell code etc...
import argparse
import atexit
import functools
import logging
import logging.handlers
import os
import pkg_resources
import sys
import time
import traceback

import configargparse
import configobj
import OpenSSL
import zope.component
import zope.interface.exceptions
import zope.interface.verify

from acme import client as acme_client
from acme import jose

import letsencrypt

from letsencrypt import account
from letsencrypt import colored_logging
from letsencrypt import configuration
from letsencrypt import constants
from letsencrypt import client
from letsencrypt import crypto_util
from letsencrypt import errors
from letsencrypt import interfaces
from letsencrypt import le_util
from letsencrypt import log
from letsencrypt import reporter
from letsencrypt import storage

from letsencrypt.display import util as display_util
from letsencrypt.display import ops as display_ops
from letsencrypt.plugins import disco as plugins_disco


logger = logging.getLogger(__name__)


# Argparse's help formatting has a lot of unhelpful peculiarities, so we want
# to replace as much of it as we can...

# This is the stub to include in help generated by argparse

SHORT_USAGE = """
  letsencrypt [SUBCOMMAND] [options] [domains]

The Let's Encrypt agent can obtain and install HTTPS/TLS/SSL certificates.  By
default, it will attempt to use a webserver both for obtaining and installing
the cert.  """

# This is the short help for letsencrypt --help, where we disable argparse
# altogether
USAGE = SHORT_USAGE + """Major SUBCOMMANDS are:

  (default) everything Obtain & install a cert in your current webserver
  auth                 Authenticate & obtain cert, but do not install it
  install              Install a previously obtained cert in a server
  revoke               Revoke a previously obtained certificate
  rollback             Rollback server configuration changes made during install
  config_changes       Show changes made to server config during installation

Choice of server for authentication/installation:

  --apache          Use the Apache plugin for authentication & installation
  --nginx           Use the Nginx plugin for authentication & installation
  --standalone      Run a standalone HTTPS server (for authentication only)
  OR:
  --authenticator standalone --installer nginx

More detailed help:

  -h, --help [topic]    print this message, or detailed help on a topic;
                        the available topics are:

   all, apache, automation, nginx, paths, security, testing, or any of the
   subcommands
"""


def _find_domains(args, installer):
    if args.domains is None:
        domains = display_ops.choose_names(installer)
    else:
        domains = args.domains

    if not domains:
        raise errors.Error("Please specify --domains, or --installer that "
                           "will help in domain names autodiscovery")

    return domains


def _determine_account(args, config):
    """Determine which account to use.

    In order to make the renewer (configuration de/serialization) happy,
    if ``args.account`` is ``None``, it will be updated based on the
    user input. Same for ``args.email``.

    :param argparse.Namespace args: CLI arguments
    :param letsencrypt.interface.IConfig config: Configuration object
    :param .AccountStorage account_storage: Account storage.

    :returns: Account and optionally ACME client API (biproduct of new
        registration).
    :rtype: `tuple` of `letsencrypt.account.Account` and
        `acme.client.Client`

    """
    account_storage = account.AccountFileStorage(config)
    acme = None

    if args.account is not None:
        acc = account_storage.load(args.account)
    else:
        accounts = account_storage.find_all()
        if len(accounts) > 1:
            acc = display_ops.choose_account(accounts)
        elif len(accounts) == 1:
            acc = accounts[0]
        else:  # no account registered yet
            if args.email is None:
                args.email = display_ops.get_email()
            if not args.email:  # get_email might return ""
                args.email = None

            def _tos_cb(regr):
                if args.tos:
                    return True
                msg = ("Please read the Terms of Service at {0}. You "
                       "must agree in order to register with the ACME "
                       "server at {1}".format(
                           regr.terms_of_service, config.server))
                return zope.component.getUtility(interfaces.IDisplay).yesno(
                    msg, "Agree", "Cancel")

            try:
                acc, acme = client.register(
                    config, account_storage, tos_cb=_tos_cb)
            except errors.Error as error:
                logger.debug(error, exc_info=True)
                raise errors.Error(
                    "Unable to register an account with ACME server")

    args.account = acc.id
    return acc, acme


def _init_le_client(args, config, authenticator, installer):
    if authenticator is not None:
        # if authenticator was given, then we will need account...
        acc, acme = _determine_account(args, config)
        logger.debug("Picked account: %r", acc)
        # XXX
        #crypto_util.validate_key_csr(acc.key)
    else:
        acc, acme = None, None

    return client.Client(config, acc, authenticator, installer, acme=acme)


def _find_duplicative_certs(domains, config, renew_config):
    """Find existing certs that duplicate the request."""

    identical_names_cert, subset_names_cert = None, None

    configs_dir = renew_config.renewal_configs_dir
    # Verify the directory is there
    le_util.make_or_verify_dir(configs_dir, mode=0o755, uid=os.geteuid())

    cli_config = configuration.RenewerConfiguration(config)
    for renewal_file in os.listdir(configs_dir):
        try:
            full_path = os.path.join(configs_dir, renewal_file)
            rc_config = configobj.ConfigObj(renew_config.renewer_config_file)
            rc_config.merge(configobj.ConfigObj(full_path))
            rc_config.filename = full_path
            candidate_lineage = storage.RenewableCert(
                rc_config, config_opts=None, cli_config=cli_config)
        except (configobj.ConfigObjError, errors.CertStorageError, IOError):
            logger.warning("Renewal configuration file %s is broken. "
                           "Skipping.", full_path)
            continue
        # TODO: Handle these differently depending on whether they are
        #       expired or still valid?
        candidate_names = set(candidate_lineage.names())
        if candidate_names == set(domains):
            identical_names_cert = candidate_lineage
        elif candidate_names.issubset(set(domains)):
            subset_names_cert = candidate_lineage

    return identical_names_cert, subset_names_cert


def _treat_as_renewal(config, domains):
    """Determine whether or not the call should be treated as a renewal.

    :returns: RenewableCert or None if renewal shouldn't occur.
    :rtype: :class:`.storage.RenewableCert`

    :raises .Error: If the user would like to rerun the client again.

    """
    renewal = False

    # Considering the possibility that the requested certificate is
    # related to an existing certificate.  (config.duplicate, which
    # is set with --duplicate, skips all of this logic and forces any
    # kind of certificate to be obtained with renewal = False.)
    if not config.duplicate:
        ident_names_cert, subset_names_cert = _find_duplicative_certs(
            domains, config, configuration.RenewerConfiguration(config))
        # I am not sure whether that correctly reads the systemwide
        # configuration file.
        question = None
        if ident_names_cert is not None:
            question = (
                "You have an existing certificate that contains exactly the "
                "same domains you requested (ref: {0}){br}{br}Do you want to "
                "renew and replace this certificate with a newly-issued one?"
            ).format(ident_names_cert.configfile.filename, br=os.linesep)
        elif subset_names_cert is not None:
            question = (
                "You have an existing certificate that contains a portion of "
                "the domains you requested (ref: {0}){br}{br}It contains these "
                "names: {1}{br}{br}You requested these names for the new "
                "certificate: {2}.{br}{br}Do you want to replace this existing "
                "certificate with the new certificate?"
            ).format(subset_names_cert.configfile.filename,
                     ", ".join(subset_names_cert.names()),
                     ", ".join(domains),
                     br=os.linesep)
        if question is None:
            # We aren't in a duplicative-names situation at all, so we don't
            # have to tell or ask the user anything about this.
            pass
        elif config.renew_by_default or zope.component.getUtility(
                interfaces.IDisplay).yesno(question, "Replace", "Cancel"):
            renewal = True
        else:
            reporter_util = zope.component.getUtility(interfaces.IReporter)
            reporter_util.add_message(
                "To obtain a new certificate that {0} an existing certificate "
                "in its domain-name coverage, you must use the --duplicate "
                "option.{br}{br}For example:{br}{br}{1} --duplicate {2}".format(
                    "duplicates" if ident_names_cert is not None else
                    "overlaps with",
                    sys.argv[0], " ".join(sys.argv[1:]),
                    br=os.linesep
                ),
                reporter_util.HIGH_PRIORITY)
            raise errors.Error(
                "User did not use proper CLI and would like "
                "to reinvoke the client.")

        if renewal:
            return ident_names_cert if ident_names_cert is not None else subset_names_cert

    return None


def _report_new_cert(cert_path):
    """Reports the creation of a new certificate to the user."""
    reporter_util = zope.component.getUtility(interfaces.IReporter)
    reporter_util.add_message("Congratulations! Your certificate has been "
                              "saved at {0}.".format(cert_path),
                              reporter_util.MEDIUM_PRIORITY)


def _auth_from_domains(le_client, config, domains, plugins):
    """Authenticate and enroll certificate."""
    # Note: This can raise errors... caught above us though.
    lineage = _treat_as_renewal(config, domains)

    if lineage is not None:
        # TODO: schoen wishes to reuse key - discussion
        # https://github.com/letsencrypt/letsencrypt/pull/777/files#r40498574
        new_certr, new_chain, new_key, _ = le_client.obtain_certificate(domains)
        # TODO: Check whether it worked! <- or make sure errors are thrown (jdk)
        lineage.save_successor(
            lineage.latest_common_version(), OpenSSL.crypto.dump_certificate(
                OpenSSL.crypto.FILETYPE_PEM, new_certr.body),
            new_key.pem, crypto_util.dump_pyopenssl_chain(new_chain))

        lineage.update_all_links_to(lineage.latest_common_version())
        # TODO: Check return value of save_successor
        # TODO: Also update lineage renewal config with any relevant
        #       configuration values from this attempt? <- Absolutely (jdkasten)
    else:
        # TREAT AS NEW REQUEST
        lineage = le_client.obtain_and_enroll_certificate(domains, plugins)
        if not lineage:
            raise errors.Error("Certificate could not be obtained")

    _report_new_cert(lineage.cert)

    return lineage


# TODO: Make run as close to auth + install as possible
# Possible difficulties: args.csr was hacked into auth
def run(args, config, plugins):  # pylint: disable=too-many-branches,too-many-locals
    """Obtain a certificate and install."""
    # Begin authenticator and installer setup
    if args.configurator is not None and (args.installer is not None or
                                          args.authenticator is not None):
        return ("Either --configurator or --authenticator/--installer"
                "pair, but not both, is allowed")

    if args.authenticator is not None or args.installer is not None:
        installer = display_ops.pick_installer(
            config, args.installer, plugins)
        authenticator = display_ops.pick_authenticator(
            config, args.authenticator, plugins)
    else:
        # TODO: this assumes that user doesn't want to pick authenticator
        #       and installer separately...
        authenticator = installer = display_ops.pick_configurator(
            config, args.configurator, plugins)

    if installer is None or authenticator is None:
        return "Configurator could not be determined"
    # End authenticator and installer setup

    domains = _find_domains(args, installer)

    # TODO: Handle errors from _init_le_client?
    le_client = _init_le_client(args, config, authenticator, installer)

    lineage = _auth_from_domains(le_client, config, domains, plugins)

    # TODO: We also need to pass the fullchain (for Nginx)
    le_client.deploy_certificate(
        domains, lineage.privkey, lineage.cert, lineage.chain)
    le_client.enhance_config(domains, args.redirect)

    if len(lineage.available_versions("cert")) == 1:
        display_ops.success_installation(domains)
    else:
        display_ops.success_renewal(domains)


def auth(args, config, plugins):
    """Authenticate & obtain cert, but do not install it."""

    if args.domains is not None and args.csr is not None:
        # TODO: --csr could have a priority, when --domains is
        # supplied, check if CSR matches given domains?
        return "--domains and --csr are mutually exclusive"

    authenticator = display_ops.pick_authenticator(
        config, args.authenticator, plugins)
    if authenticator is None:
        return "Authenticator could not be determined"

    if args.installer is not None:
        installer = display_ops.pick_installer(config, args.installer, plugins)
    else:
        installer = None

    # TODO: Handle errors from _init_le_client?
    le_client = _init_le_client(args, config, authenticator, installer)

    # This is a special case; cert and chain are simply saved
    if args.csr is not None:
        certr, chain = le_client.obtain_certificate_from_csr(le_util.CSR(
            file=args.csr[0], data=args.csr[1], form="der"))
        le_client.save_certificate(
            certr, chain, args.cert_path, args.chain_path)
        _report_new_cert(args.cert_path)
    else:
        domains = _find_domains(args, installer)
        _auth_from_domains(le_client, config, domains, plugins)


def install(args, config, plugins):
    """Install a previously obtained cert in a server."""
    # XXX: Update for renewer/RenewableCert
    installer = display_ops.pick_installer(config, args.installer, plugins)
    if installer is None:
        return "Installer could not be determined"
    domains = _find_domains(args, installer)
    le_client = _init_le_client(
        args, config, authenticator=None, installer=installer)
    assert args.cert_path is not None  # required=True in the subparser
    le_client.deploy_certificate(
        domains, args.key_path, args.cert_path, args.chain_path)
    le_client.enhance_config(domains, args.redirect)


def revoke(args, config, unused_plugins):  # TODO: coop with renewal config
    """Revoke a previously obtained certificate."""
    if args.key_path is not None:  # revocation by cert key
        logger.debug("Revoking %s using cert key %s",
                     args.cert_path[0], args.key_path[0])
        acme = acme_client.Client(
            config.server, key=jose.JWK.load(args.key_path[1]))
    else:  # revocation by account key
        logger.debug("Revoking %s using Account Key", args.cert_path[0])
        acc, _ = _determine_account(args, config)
        # pylint: disable=protected-access
        acme = client._acme_from_config_key(config, acc.key)
    acme.revoke(jose.ComparableX509(crypto_util.pyopenssl_load_certificate(
        args.cert_path[1])[0]))


def rollback(args, config, plugins):
    """Rollback server configuration changes made during install."""
    client.rollback(args.installer, args.checkpoints, config, plugins)


def config_changes(unused_args, config, unused_plugins):
    """Show changes made to server config during installation

    View checkpoints and associated configuration changes.

    """
    client.view_config_changes(config)


def plugins_cmd(args, config, plugins):  # TODO: Use IDisplay rather than print
    """List server software plugins."""
    logger.debug("Expected interfaces: %s", args.ifaces)

    ifaces = [] if args.ifaces is None else args.ifaces
    filtered = plugins.visible().ifaces(ifaces)
    logger.debug("Filtered plugins: %r", filtered)

    if not args.init and not args.prepare:
        print str(filtered)
        return

    filtered.init(config)
    verified = filtered.verify(ifaces)
    logger.debug("Verified plugins: %r", verified)

    if not args.prepare:
        print str(verified)
        return

    verified.prepare()
    available = verified.available()
    logger.debug("Prepared plugins: %s", available)
    print str(available)


def read_file(filename, mode="rb"):
    """Returns the given file's contents.

    :param str filename: Filename
    :param str mode: open mode (see `open`)

    :returns: A tuple of filename and its contents
    :rtype: tuple

    :raises argparse.ArgumentTypeError: File does not exist or is not readable.

    """
    try:
        return filename, open(filename, mode).read()
    except IOError as exc:
        raise argparse.ArgumentTypeError(exc.strerror)


def flag_default(name):
    """Default value for CLI flag."""
    return constants.CLI_DEFAULTS[name]


def config_help(name, hidden=False):
    """Help message for `.IConfig` attribute."""
    if hidden:
        return argparse.SUPPRESS
    else:
        return interfaces.IConfig[name].__doc__


class SilentParser(object):  # pylint: disable=too-few-public-methods
    """Silent wrapper around argparse.

    A mini parser wrapper that doesn't print help for its
    arguments. This is needed for the use of callbacks to define
    arguments within plugins.

    """
    def __init__(self, parser):
        self.parser = parser

    def add_argument(self, *args, **kwargs):
        """Wrap, but silence help"""
        kwargs["help"] = argparse.SUPPRESS
        self.parser.add_argument(*args, **kwargs)


class HelpfulArgumentParser(object):
    """Argparse Wrapper.

    This class wraps argparse, adding the ability to make --help less
    verbose, and request help on specific subcategories at a time, eg
    'letsencrypt --help security' for security options.

    """
    def __init__(self, args, plugins):
        plugin_names = [name for name, _p in plugins.iteritems()]
        self.help_topics = HELP_TOPICS + plugin_names + [None]
        self.parser = configargparse.ArgParser(
            usage=SHORT_USAGE,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            default_config_files=flag_default("config_files"))

        # This is the only way to turn off overly verbose config flag documentation
        self.parser._add_config_file_help = False  # pylint: disable=protected-access
        self.silent_parser = SilentParser(self.parser)

        self.verb = None
        self.args = self.preprocess_args(args)
        help1 = self.prescan_for_flag("-h", self.help_topics)
        help2 = self.prescan_for_flag("--help", self.help_topics)
        assert max(True, "a") == "a", "Gravity changed direction"
        help_arg = max(help1, help2)
        if help_arg is True:
            # just --help with no topic; avoid argparse altogether
            print USAGE
            sys.exit(0)
        self.visible_topics = self.determine_help_topics(help_arg)
        #print self.visible_topics
        self.groups = {}  # elements are added by .add_group()

    def preprocess_args(self, args):
        """Work around some limitations in argparse.

        Currently: add the default verb "run" as a default, and ensure that the
        subcommand / verb comes last.
        """
        if "-h" in args or "--help" in args:
            # all verbs double as help arguments; don't get them confused
            self.verb = "help"
            return args

        for i, token in enumerate(args):
            if token in VERBS:
                reordered = args[:i] + args[i+1:] + [args[i]]
                self.verb = token
                return reordered

        self.verb = "run"
        return args + ["run"]

    def prescan_for_flag(self, flag, possible_arguments):
        """Checks cli input for flags.

        Check for a flag, which accepts a fixed set of possible arguments, in
        the command line; we will use this information to configure argparse's
        help correctly.  Return the flag's argument, if it has one that matches
        the sequence @possible_arguments; otherwise return whether the flag is
        present.

        """
        if flag not in self.args:
            return False
        pos = self.args.index(flag)
        try:
            nxt = self.args[pos + 1]
            if nxt in possible_arguments:
                return nxt
        except IndexError:
            pass
        return True

    def add(self, topic, *args, **kwargs):
        """Add a new command line argument.

        @topic is required, to indicate which part of the help will document
        it, but can be None for `always documented'.

        """
        if self.visible_topics[topic]:
            if topic in self.groups:
                group = self.groups[topic]
                group.add_argument(*args, **kwargs)
            else:
                self.parser.add_argument(*args, **kwargs)
        else:
            kwargs["help"] = argparse.SUPPRESS
            self.parser.add_argument(*args, **kwargs)

    def add_group(self, topic, **kwargs):
        """

        This has to be called once for every topic; but we leave those calls
        next to the argument definitions for clarity. Return something
        arguments can be added to if necessary, either the parser or an argument
        group.

        """
        if self.visible_topics[topic]:
            #print "Adding visible group " + topic
            group = self.parser.add_argument_group(topic, **kwargs)
            self.groups[topic] = group
            return group
        else:
            #print "Invisible group " + topic
            return self.silent_parser

    def add_plugin_args(self, plugins):
        """

        Let each of the plugins add its own command line arguments, which
        may or may not be displayed as help topics.

        """
        for name, plugin_ep in plugins.iteritems():
            parser_or_group = self.add_group(name, description=plugin_ep.description)
            #print parser_or_group
            plugin_ep.plugin_cls.inject_parser_options(parser_or_group, name)

    def determine_help_topics(self, chosen_topic):
        """

        The user may have requested help on a topic, return a dict of which
        topics to display. @chosen_topic has prescan_for_flag's return type

        :returns: dict

        """
        # topics maps each topic to whether it should be documented by
        # argparse on the command line
        if chosen_topic == "all":
            return dict([(t, True) for t in self.help_topics])
        elif not chosen_topic:
            return dict([(t, False) for t in self.help_topics])
        else:
            return dict([(t, t == chosen_topic) for t in self.help_topics])


def create_parser(plugins, args):
    """Create parser."""
    helpful = HelpfulArgumentParser(args, plugins)

    # --help is automatically provided by argparse
    helpful.add(
        None, "-v", "--verbose", dest="verbose_count", action="count",
        default=flag_default("verbose_count"), help="This flag can be used "
        "multiple times to incrementally increase the verbosity of output, "
        "e.g. -vvv.")
    helpful.add(
        None, "-t", "--text", dest="text_mode", action="store_true",
        help="Use the text output instead of the curses UI.")
    helpful.add(None, "-m", "--email", help=config_help("email"))
    # positional arg shadows --domains, instead of appending, and
    # --domains is useful, because it can be stored in config
    #for subparser in parser_run, parser_auth, parser_install:
    #    subparser.add_argument("domains", nargs="*", metavar="domain")
    helpful.add(None, "-d", "--domains", metavar="DOMAIN", action="append")
    helpful.add(
        None, "--duplicate", dest="duplicate", action="store_true",
        help="Allow getting a certificate that duplicates an existing one")

    helpful.add_group(
        "automation",
        description="Arguments for automating execution & other tweaks")
    helpful.add(
        "automation", "--version", action="version",
        version="%(prog)s {0}".format(letsencrypt.__version__),
        help="show program's version number and exit")
    helpful.add(
        "automation", "--renew-by-default", action="store_true",
        help="Select renewal by default when domains are a superset of a "
             "a previously attained cert")
    helpful.add(
        "automation", "--agree-eula", dest="eula", action="store_true",
        help="Agree to the Let's Encrypt Developer Preview EULA")
    helpful.add(
        "automation", "--agree-tos", dest="tos", action="store_true",
        help="Agree to the Let's Encrypt Subscriber Agreement")
    helpful.add(
        "automation", "--account", metavar="ACCOUNT_ID",
        help="Account ID to use")

    helpful.add_group(
        "testing", description="The following flags are meant for "
        "testing purposes only! Do NOT change them, unless you "
        "really know what you're doing!")
    helpful.add(
        "testing", "--debug", action="store_true",
        help="Show tracebacks if the program exits abnormally")
    helpful.add(
        "testing", "--no-verify-ssl", action="store_true",
        help=config_help("no_verify_ssl"),
        default=flag_default("no_verify_ssl"))
    helpful.add(  # TODO: apache plugin does NOT respect it (#479)
        "testing", "--dvsni-port", type=int, default=flag_default("dvsni_port"),
        help=config_help("dvsni_port"))
    helpful.add("testing", "--simple-http-port", type=int,
                help=config_help("simple_http_port"))
    helpful.add("testing", "--no-simple-http-tls", action="store_true",
                help=config_help("no_simple_http_tls"))

    helpful.add_group(
        "security", description="Security parameters & server settings")
    helpful.add(
        "security", "-B", "--rsa-key-size", type=int, metavar="N",
        default=flag_default("rsa_key_size"), help=config_help("rsa_key_size"))
    # TODO: resolve - assumes binary logic while client.py assumes ternary.
    helpful.add(
        "security", "-r", "--redirect", action="store_true",
        help="Automatically redirect all HTTP traffic to HTTPS for the newly "
             "authenticated vhost.")
    helpful.add(
        "security", "--strict-permissions", action="store_true",
        help="Require that all configuration files are owned by the current "
             "user; only needed if your config is somewhere unsafe like /tmp/")

    _paths_parser(helpful)
    # _plugins_parsing should be the last thing to act upon the main
    # parser (--help should display plugin-specific options last)
    _plugins_parsing(helpful, plugins)

    _create_subparsers(helpful)

    return helpful.parser, helpful.args

# For now unfortunately this constant just needs to match the code below;
# there isn't an elegant way to autogenerate it in time.
VERBS = ["run", "auth", "install", "revoke", "rollback", "config_changes",
         "plugins"]

HELP_TOPICS = (["all", "security", "paths", "automation", "testing", "apache", "nginx"] +
               [v for v in VERBS])


def _create_subparsers(helpful):
    subparsers = helpful.parser.add_subparsers(metavar="SUBCOMMAND")

    def add_subparser(name, func):  # pylint: disable=missing-docstring
        subparser = subparsers.add_parser(
            name, help=func.__doc__.splitlines()[0], description=func.__doc__)
        subparser.set_defaults(func=func)
        return subparser

    # the order of add_subparser() calls is important: it defines the
    # order in which subparser names will be displayed in --help
    # these add_subparser objects return objects to which arguments could be
    # attached, but they have annoying arg ordering constrains so we use
    # groups instead: https://github.com/letsencrypt/letsencrypt/issues/820

    add_subparser("run", run)
    add_subparser("auth", auth)
    helpful.add_group("auth", description="Options for modifying how a cert is obtained")
    add_subparser("install", install)
    helpful.add_group("install", description="Options for modifying how a cert is deployed")
    add_subparser("revoke", revoke)
    helpful.add_group("revoke", description="Options for revocation of certs")
    add_subparser("rollback", rollback)
    helpful.add_group("rollback", description="Options for reverting config changes")
    add_subparser("config_changes", config_changes)
    add_subparser("plugins", plugins_cmd)
    helpful.add_group("plugins", description="Plugin options")

    helpful.add("auth",
        "--csr", type=read_file, help="Path to a Certificate Signing Request (CSR) in DER format.")
    helpful.add("rollback",
        "--checkpoints", type=int, metavar="N",
        default=flag_default("rollback_checkpoints"),
        help="Revert configuration N number of checkpoints.")

    helpful.add("plugins",
        "--init", action="store_true", help="Initialize plugins.")
    helpful.add("plugins",
        "--prepare", action="store_true", help="Initialize and prepare plugins.")
    helpful.add("plugins",
        "--authenticators", action="append_const", dest="ifaces",
        const=interfaces.IAuthenticator, help="Limit to authenticator plugins only.")
    helpful.add("plugins",
        "--installers", action="append_const", dest="ifaces",
        const=interfaces.IInstaller, help="Limit to installer plugins only.")


def _paths_parser(helpful):
    add = helpful.add
    verb = helpful.verb
    helpful.add_group(
        "paths", description="Arguments changing execution paths & servers")

    cph = "Path to where cert is saved (with auth), installed (with install --csr) or revoked."
    if verb == "auth":
        add("paths", "--cert-path", default=flag_default("auth_cert_path"), help=cph)
    elif verb == "revoke":
        add("paths", "--cert-path", type=read_file, required=True, help=cph)
    else:
        add("paths", "--cert-path", help=cph, required=(verb == "install"))

    # revoke --key-path reads a file, install --key-path takes a string
    add("paths", "--key-path", type=((verb == "revoke" and read_file) or str),
        required=(verb == "install"),
        help="Path to private key for cert creation or revocation (if account key is missing)")

    default_cp = None
    if verb == "auth":
        default_cp = flag_default("auth_chain_path")
    add("paths", "--chain-path", default=default_cp,
        help="Accompanying path to a certificate chain.")
    add("paths", "--config-dir", default=flag_default("config_dir"),
        help=config_help("config_dir"))
    add("paths", "--work-dir", default=flag_default("work_dir"),
        help=config_help("work_dir"))
    add("paths", "--logs-dir", default=flag_default("logs_dir"),
        help="Logs directory.")
    add("paths", "--server", default=flag_default("server"),
        help=config_help("server"))


def _plugins_parsing(helpful, plugins):
    helpful.add_group(
        "plugins", description="Let's Encrypt client supports an "
        "extensible plugins architecture. See '%(prog)s plugins' for a "
        "list of all available plugins and their names. You can force "
        "a particular plugin by setting options provided below. Further "
        "down this help message you will find plugin-specific options "
        "(prefixed by --{plugin_name}).")
    helpful.add(
        "plugins", "-a", "--authenticator", help="Authenticator plugin name.")
    helpful.add(
        "plugins", "-i", "--installer", help="Installer plugin name.")
    helpful.add(
        "plugins", "--configurator", help="Name of the plugin that is "
        "both an authenticator and an installer. Should not be used "
        "together with --authenticator or --installer.")

    # things should not be reorder past/pre this comment:
    # plugins_group should be displayed in --help before plugin
    # specific groups (so that plugins_group.description makes sense)

    helpful.add_plugin_args(plugins)


def _setup_logging(args):
    level = -args.verbose_count * 10
    fmt = "%(asctime)s:%(levelname)s:%(name)s:%(message)s"
    if args.text_mode:
        handler = colored_logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt))
    else:
        handler = log.DialogHandler()
        # dialog box is small, display as less as possible
        handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(level)

    # TODO: use fileConfig?

    # unconditionally log to file for debugging purposes
    # TODO: change before release?
    log_file_name = os.path.join(args.logs_dir, 'letsencrypt.log')
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_name, maxBytes=2 ** 20, backupCount=10)
    # rotate on each invocation, rollover only possible when maxBytes
    # is nonzero and backupCount is nonzero, so we set maxBytes as big
    # as possible not to overrun in single CLI invocation (1MB).
    file_handler.doRollover()  # TODO: creates empty letsencrypt.log.1 file
    file_handler.setLevel(logging.DEBUG)
    file_handler_formatter = logging.Formatter(fmt=fmt)
    file_handler_formatter.converter = time.gmtime  # don't use localtime
    file_handler.setFormatter(file_handler_formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # send all records to handlers
    root_logger.addHandler(handler)
    root_logger.addHandler(file_handler)

    logger.debug("Root logging level set at %d", level)
    logger.info("Saving debug log to %s", log_file_name)


def _handle_exception(exc_type, exc_value, trace, args):
    """Logs exceptions and reports them to the user.

    Args is used to determine how to display exceptions to the user. In
    general, if args.debug is True, then the full exception and traceback is
    shown to the user, otherwise it is suppressed. If args itself is None,
    then the traceback and exception is attempted to be written to a logfile.
    If this is successful, the traceback is suppressed, otherwise it is shown
    to the user. sys.exit is always called with a nonzero status.

    """
    logger.debug(
        "Exiting abnormally:%s%s",
        os.linesep,
        "".join(traceback.format_exception(exc_type, exc_value, trace)))

    if issubclass(exc_type, Exception) and (args is None or not args.debug):
        if args is None:
            logfile = "letsencrypt.log"
            try:
                with open(logfile, "w") as logfd:
                    traceback.print_exception(
                        exc_type, exc_value, trace, file=logfd)
            except:  # pylint: disable=bare-except
                sys.exit("".join(
                    traceback.format_exception(exc_type, exc_value, trace)))

        if issubclass(exc_type, errors.Error):
            sys.exit(exc_value)
        else:
            # Tell the user a bit about what happened, without overwhelming
            # them with a full traceback
            msg = ("An unexpected error occurred.\n" +
                   traceback.format_exception_only(exc_type, exc_value)[0] +
                   "Please see the ")
            if args is None:
                msg += "logfile '{0}' for more details.".format(logfile)
            else:
                msg += "logfiles in {0} for more details.".format(args.logs_dir)
            sys.exit(msg)
    else:
        sys.exit("".join(
            traceback.format_exception(exc_type, exc_value, trace)))


def main(cli_args=sys.argv[1:]):
    """Command line argument parsing and main script execution."""
    sys.excepthook = functools.partial(_handle_exception, args=None)

    # note: arg parser internally handles --help (and exits afterwards)
    plugins = plugins_disco.PluginsRegistry.find_all()
    parser, tweaked_cli_args = create_parser(plugins, cli_args)
    args = parser.parse_args(tweaked_cli_args)
    config = configuration.NamespaceConfig(args)
    zope.component.provideUtility(config)

    # Setup logging ASAP, otherwise "No handlers could be found for
    # logger ..." TODO: this should be done before plugins discovery
    for directory in config.config_dir, config.work_dir:
        le_util.make_or_verify_dir(
            directory, constants.CONFIG_DIRS_MODE, os.geteuid(),
            "--strict-permissions" in cli_args)
    # TODO: logs might contain sensitive data such as contents of the
    # private key! #525
    le_util.make_or_verify_dir(
        args.logs_dir, 0o700, os.geteuid(), "--strict-permissions" in cli_args)
    _setup_logging(args)

    # do not log `args`, as it contains sensitive data (e.g. revoke --key)!
    logger.debug("Arguments: %r", cli_args)
    logger.debug("Discovered plugins: %r", plugins)

    sys.excepthook = functools.partial(_handle_exception, args=args)

    # Displayer
    if args.text_mode:
        displayer = display_util.FileDisplay(sys.stdout)
    else:
        displayer = display_util.NcursesDisplay()
    zope.component.provideUtility(displayer)

    # Reporter
    report = reporter.Reporter()
    zope.component.provideUtility(report)
    atexit.register(report.atexit_print_messages)

    # TODO: remove developer EULA prompt for the launch
    if not config.eula:
        eula = pkg_resources.resource_string("letsencrypt", "EULA")
        if not zope.component.getUtility(interfaces.IDisplay).yesno(
                eula, "Agree", "Cancel"):
            raise errors.Error("Must agree to TOS")

    if not os.geteuid() == 0:
        logger.warning(
            "Root (sudo) is required to run most of letsencrypt functionality.")
        # check must be done after arg parsing as --help should work
        # w/o root; on the other hand, e.g. "letsencrypt run
        # --authenticator dns" or "letsencrypt plugins" does not
        # require root as well
        #return (
        #    "{0}Root is required to run letsencrypt.  Please use sudo.{0}"
        #    .format(os.linesep))

    return args.func(args, config, plugins)


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
