# -*- coding: utf-8 -*-

# Copyright 2014 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ConfigParser
import logging
import logging.handlers
import os

import twisted.internet.reactor
from twisted.python import log

from db.sqlitedb import SqliteDatabase

from netaddr import IPSet, IPNetwork

from http.httpcommon import SslComponents
from http.httpserver import (
    ClientApiHttpServer, ReplicationHttpsServer,
    InternalApiHttpServer,
)
from http.httpsclient import ReplicationHttpsClient
from http.servlets.blindlysignstuffservlet import BlindlySignStuffServlet
from http.servlets.pubkeyservlets import EphemeralPubkeyIsValidServlet, PubkeyIsValidServlet
from validators.emailvalidator import EmailValidator
from validators.msisdnvalidator import MsisdnValidator
from hs_federation.verifier import Verifier

from sign.ed25519 import SydentEd25519

from http.servlets.emailservlet import EmailRequestCodeServlet, EmailValidateCodeServlet
from http.servlets.msisdnservlet import MsisdnRequestCodeServlet, MsisdnValidateCodeServlet
from http.servlets.lookupservlet import LookupServlet
from http.servlets.bulklookupservlet import BulkLookupServlet
from http.servlets.pubkeyservlets import Ed25519Servlet
from http.servlets.threepidbindservlet import ThreePidBindServlet
from http.servlets.threepidunbindservlet import ThreePidUnbindServlet
from http.servlets.replication import ReplicationPushServlet
from http.servlets.getvalidated3pidservlet import GetValidated3pidServlet
from http.servlets.store_invite_servlet import StoreInviteServlet
from http.servlets.infoservlet import InfoServlet
from http.servlets.internalinfoservlet import InternalInfoServlet
from http.servlets.profilereplicationservlet import ProfileReplicationServlet
from http.servlets.userdirectorysearchservlet import UserDirectorySearchServlet
from http.servlets.v1_servlet import V1Servlet
from http.info import Info

from threepid.bind import ThreepidBinder

from replication.pusher import Pusher

logger = logging.getLogger(__name__)

def list_from_comma_sep_string(rawstr):
    if rawstr == '':
        return []
    return [x.strip() for x in rawstr.split(',')]


CONFIG_DEFAULTS = {
    'general': {
        'server.name': '',
        'log.path': '',
        'pidfile.path': 'sydent.pid',
        'shadow.hs.master': '',
        'shadow.hs.slave': '',
        'ips.nonshadow': '',  # comma separated list of CIDR ranges which /info will return non-shadow HS to.
        # Timestamp in milliseconds, or string in the form of e.g. "2w" for two weeks,
        # which defines the time during which an invite will be valid on this server
        # from the time it has been received.
        'invites.validity_period': '',
        # Path to file detailing the configuration of the /info and /internal-info servlets.
        # More information can be found in docs/info.md.
        'info_path': 'info.yaml',
    },
    'db': {
        'db.file': 'sydent.db',
    },
    'http': {
        'clientapi.http.bind_address': '::',
        'clientapi.http.port': '8090',
        # internalapi.http.bind_address defaults to '::1'
        'internalapi.http.port': '',
        'replication.https.certfile': '',
        'replication.https.cacert': '', # This should only be used for testing
        'replication.https.bind_address': '::',
        'replication.https.port': '4434',
        'obey_x_forwarded_for': 'False',
    },
    'email': {
        'email.template': 'res/email.template',
        'email.from': 'Sydent Validation <noreply@{hostname}>',
        'email.subject': 'Your Validation Token',
        'email.invite.subject': '%(sender_display_name)s has invited you to chat',
        'email.smtphost': 'localhost',
        'email.smtpport': '25',
        'email.smtpusername': '',
        'email.smtppassword': '',
        'email.hostname': '',
        'email.tlsmode': '0',
    },
    'sms': {
        'bodyTemplate': 'Your code is {token}',
    },
    'crypto': {
        'ed25519.signingkey': '',
    },
    'userdir': {
        'userdir.allowed_homeservers': '',
    },
}


class Sydent:
    def __init__(self):
        self.config_file = os.environ.get('SYDENT_CONF', "sydent.conf")
        self.cfg = parse_config(self.config_file)

        log_format = (
            "%(asctime)s - %(name)s - %(lineno)d - %(levelname)s"
            " - %(message)s"
        )
        formatter = logging.Formatter(log_format)

        logPath = self.cfg.get('general', "log.path")
        if logPath != '':
            handler = logging.handlers.TimedRotatingFileHandler(
                logPath, when='midnight', backupCount=365
            )
            handler.setFormatter(formatter)
            def sighup(signum, stack):
                logger.info("Closing log file due to SIGHUP")
                handler.doRollover()
                logger.info("Opened new log file due to SIGHUP")
        else:
            handler = logging.StreamHandler()

        handler.setFormatter(formatter)
        rootLogger = logging.getLogger('')
        rootLogger.setLevel(logging.INFO)
        rootLogger.addHandler(handler)

        logger.info("Starting Sydent server")

        self.pidfile = self.cfg.get('general', "pidfile.path");

        self.nonshadow_ips = None
        ips = self.cfg.get('general', "ips.nonshadow");
        if ips:
            self.nonshadow_ips = IPSet()
            ips = ips.split(',')
            for ip in ips:
                self.nonshadow_ips.add(IPNetwork(ip))

        observer = log.PythonLoggingObserver()
        observer.start()

        self.db = SqliteDatabase(self).db

        self.server_name = self.cfg.get('general', 'server.name')
        if self.server_name == '':
            self.server_name = os.uname()[1]
            logger.warn(("You had not specified a server name. I have guessed that this server is called '%s' "
                        + " and saved this in the config file. If this is incorrect, you should edit server.name in "
                        + "the config file.") % (self.server_name,))
            self.cfg.set('general', 'server.name', self.server_name)
            self.save_config()

        self.shadow_hs_master = self.cfg.get('general', 'shadow.hs.master')
        self.shadow_hs_slave  = self.cfg.get('general', 'shadow.hs.slave')

        self.user_dir_allowed_hses = set(list_from_comma_sep_string(
            self.cfg.get('userdir', 'userdir.allowed_homeservers', '')
        ))

        self.invites_validity_period = parse_duration(
            self.cfg.get('general', 'invites.validity_period'),
        )

        self.validators = Validators()
        self.validators.email = EmailValidator(self)
        self.validators.msisdn = MsisdnValidator(self)

        self.keyring = Keyring()
        self.keyring.ed25519 = SydentEd25519(self).signing_key
        self.keyring.ed25519.alg = 'ed25519'

        self.sig_verifier = Verifier(self)

        self.servlets = Servlets()
        self.servlets.v1 = V1Servlet(self)
        self.servlets.emailRequestCode = EmailRequestCodeServlet(self)
        self.servlets.emailValidate = EmailValidateCodeServlet(self)
        self.servlets.msisdnRequestCode = MsisdnRequestCodeServlet(self)
        self.servlets.msisdnValidate = MsisdnValidateCodeServlet(self)
        self.servlets.lookup = LookupServlet(self)
        self.servlets.bulk_lookup = BulkLookupServlet(self)
        self.servlets.pubkey_ed25519 = Ed25519Servlet(self)
        self.servlets.pubkeyIsValid = PubkeyIsValidServlet(self)
        self.servlets.ephemeralPubkeyIsValid = EphemeralPubkeyIsValidServlet(self)
        self.servlets.threepidBind = ThreePidBindServlet(self)
        self.servlets.threepidUnbind = ThreePidUnbindServlet(self)
        self.servlets.replicationPush = ReplicationPushServlet(self)
        self.servlets.getValidated3pid = GetValidated3pidServlet(self)
        self.servlets.storeInviteServlet = StoreInviteServlet(self)
        self.servlets.blindlySignStuffServlet = BlindlySignStuffServlet(self)
        self.servlets.profileReplicationServlet = ProfileReplicationServlet(self)
        self.servlets.userDirectorySearchServlet = UserDirectorySearchServlet(self)

        info = Info(self, self.cfg.get("general", "info_path"))
        self.servlets.info = InfoServlet(self, info)
        self.servlets.internalInfo = InternalInfoServlet(self, info)

        self.threepidBinder = ThreepidBinder(self, info)

        self.sslComponents = SslComponents(self)

        self.clientApiHttpServer = ClientApiHttpServer(self)
        self.replicationHttpsServer = ReplicationHttpsServer(self)
        self.replicationHttpsClient = ReplicationHttpsClient(self)

        self.pusher = Pusher(self)

    def save_config(self):
        fp = open(self.config_file, 'w')
        self.cfg.write(fp)
        fp.close()

    def run(self):
        self.clientApiHttpServer.setup()
        self.replicationHttpsServer.setup()
        self.pusher.setup()

        internalport = self.cfg.get('http', 'internalapi.http.port')
        if internalport:
            try:
                interface = self.cfg.get('http', 'internalapi.http.bind_address')
            except ConfigParser.NoOptionError:
                interface = '::1'
            self.internalApiHttpServer = InternalApiHttpServer(self)
            self.internalApiHttpServer.setup(interface, int(internalport))

        if self.pidfile:
            with open(self.pidfile, 'w') as pidfile:
                pidfile.write(str(os.getpid()) + "\n")

        twisted.internet.reactor.run()

    def ip_from_request(self, request):
        if (self.cfg.get('http', 'obey_x_forwarded_for') and
                request.requestHeaders.hasHeader("X-Forwarded-For")):
            return request.requestHeaders.getRawHeaders("X-Forwarded-For")[0]
        return request.getClientIP()


class Validators:
    pass


class Servlets:
    pass


class Keyring:
    pass


def parse_config(config_file):
    """Parse the given config file, populating missing items and sections

    Args:
        config_file (str): the file to be parsed
    """

    cfg = ConfigParser.SafeConfigParser()

    # if the config file doesn't exist, prepopulate the config object
    # with the defaults, in the right section.
    if not os.path.exists(config_file):
        for sect, entries in CONFIG_DEFAULTS.items():
            cfg.add_section(sect)
            for k, v in entries.items():
                cfg.set(sect, k, v)
    else:
        # otherwise, we have to put the defaults in the DEFAULT section,
        # to ensure that they don't override anyone's settings which are
        # in their config file in the default section (which is likely,
        # because sydent used to be braindead).
        for sect, entries in CONFIG_DEFAULTS.items():
            cfg.add_section(sect)
            for k, v in entries.items():
                cfg.set(ConfigParser.DEFAULTSECT, k, v)

        cfg.read(config_file)

    return cfg


def parse_duration(value):
    if not len(value):
        return None

    try:
        return int(value)
    except ValueError:
        pass

    second = 1000
    minute = 60 * second
    hour = 60 * minute
    day = 24 * hour
    week = 7 * day
    year = 365 * day
    sizes = {"s": second, "m": minute, "h": hour, "d": day, "w": week, "y": year}
    size = 1
    suffix = value[-1]
    if suffix in sizes:
        value = value[:-1]
        size = sizes[suffix]
    return int(value) * size


if __name__ == '__main__':
    syd = Sydent()
    syd.run()
