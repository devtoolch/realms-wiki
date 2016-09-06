from __future__ import absolute_import

import logging
import ldap3

from flask import render_template, current_app
from flask_login import login_user, logout_user

from realms.modules.auth.models import BaseUser
from .forms import LoginForm


class User(BaseUser):
    ldap_users = {}
    type = 'ldap'

    def __init__(self, userid, password, email=""):
        self.id = userid
        self.username = userid
        self.password = password
        self.email = email if email else userid

    def __repr__(self):
        return "User(userid='{}', username='{}',password='{}', email='{}')".format(
            self.id, self.username, self.password, self.email
        )

    @property
    def auth_token_id(self):
        return self.password

    @staticmethod
    def load_user(*args, **kwargs):
        return User.ldap_users.get(args[0])

    def save(self):
        self.ldap_users[self.id] = self

    @classmethod
    def get_by_userid(cls, userid):
        return cls.ldap_users.get(userid)

    @staticmethod
    def auth(userid, password):
        ldap_attrs = LdapConn(current_app.config['LDAP'], userid, password).check()
        if ldap_attrs is None:
            # something failed: authentication, connection, binding... check the logs
            return False
        user = User(userid, password)
        # add the required LDAP attributes to the user object
        for attr_name, attr_value in ldap_attrs.items():
            user.__setattr__(attr_name, attr_value)

        logging.getLogger("realms.auth.ldap").debug("Logged in: {}".format(repr(user)))

        user.save()
        login_user(user, remember=False)
        return user

    @classmethod
    def logout(cls):
        logout_user()

    @staticmethod
    def login_form():
        form = LoginForm()
        return render_template('auth/ldap/login.html', form=form)


class LdapConn(object):
    def __init__(self, config, userid, password):
        self.config = config
        self.tls = None
        self.setup_tls()
        self.server = ldap3.Server(self.config['URI'], tls=self.tls)
        self.userid = userid
        self.password = password
        self.version = int(self.config.get("OPTIONS", {}).get("OPT_PROTOCOL_VERSION", 3))
        self.conn = None

    def check(self):
        if 'USER_SEARCH' in self.config:
            return self.bind_search()
        else:
            return self.direct_bind()

    def close(self):
        if self.conn:
            if self.conn.bound:
                self.conn.unbind()

    def setup_tls(self):
        # todo !
        pass

    def start_tls(self):
        logger = logging.getLogger("realms.auth.ldap")
        if self.tls:
            try:
                self.conn.open()
                self.conn.start_tls()
            except ldap3.LDAPStartTLSError:
                logger.exception("START_TLS error")
                return False
            except Exception:
                logger.exception("START_TLS unexpectedly failed")
                return False
        return True

    def direct_bind(self):
        logger = logging.getLogger("realms.auth.ldap")
        bind_dn = self.config['BIND_DN'] % {'username': self.userid}
        self.conn = ldap3.Connection(
            self.server,
            user=bind_dn,
            password=self.password,
            version=self.version
        )

        if not self.start_tls():
            # START_TLS was demanded but it failed
            return None

        if not self.conn.bind():
            logger.info("Invalid credentials for '{}'".format(self.userid))
            return None

        logger.debug("Successfull BIND for '{}'".format(bind_dn))

        try:
            attrs = {}
            if self.conn.search(
                bind_dn,                                       # base: the user DN
                "({})".format(bind_dn.split(",", 1)[0]),       # filter: (uid=...)
                attributes=ldap3.ALL_ATTRIBUTES,
                search_scope=ldap3.BASE
            ):
                attrs = self._get_attributes(self.conn.response)
            return attrs
        finally:
            self.close()

    def bind_search(self):
        logger = logging.getLogger("realms.auth.ldap")
        bind_dn = self.config.get('BIND_DN') or None
        base_dn = self.config['USER_SEARCH']['base']
        filtr = self.config['USER_SEARCH']['filter'] % {'username': self.userid}
        scope = self.config['USER_SEARCH'].get('scope', 'subtree').lower().strip()
        if scope == "level":
            scope = ldap3.LEVEL
        elif scope == "base":
            scope = ldap3.BASE
        else:
            scope = ldap3.SUBTREE

        self.conn = ldap3.Connection(
            self.server,
            user=bind_dn,
            password=self.config.get('BIND_AUTH') or None,
            version=self.version
        )

        if not self.start_tls():
            return None

        if not self.conn.bind():
            logger.error("Can't bind to the LDAP server with provided credentials ({})'".format(bind_dn))
            return None

        logger.debug("Successfull BIND for '{}'".format(bind_dn))

        try:
            if not self.conn.search(base_dn, filtr, attributes=ldap3.ALL_ATTRIBUTES, search_scope=scope):
                logger.info("User was not found in LDAP: '{}'".format(self.userid))
                return None

            return self._get_attributes(self.conn.response)
        finally:
            self.close()

    def _get_attributes(self, resp):
        attrs = {}
        ldap_attrs = resp[0]['attributes']
        for attrname, ldap_attrname in self.config['KEY_MAP'].items():
            if ldap_attrs.get(ldap_attrname):
                # ldap attributes are multi-valued, we only return the first one
                attrs[attrname] = ldap_attrs.get(ldap_attrname)[0]
        return attrs
