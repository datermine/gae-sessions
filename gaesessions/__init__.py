"""A fast, lightweight, and secure session WSGI middleware for use with GAE."""
from Cookie import CookieError, SimpleCookie
import datetime
import hashlib
import logging
import pickle
import os

from google.appengine.api import memcache
from google.appengine.ext import db

COOKIE_PATH     = "/"
COOKIE_LIFETIME = datetime.timedelta(days=7)

_current_session = None
def get_current_session():
    return _current_session

class SessionModel(db.Model):
    """Contains session data.  key_name is the session ID and pdump contains a
    pickled dictionary which maps session variables to their values."""
    pdump = db.BlobProperty()

class Session(object):
    """Manages loading, user reading/writing, and saving of a session."""
    def __init__(self):
        self.sid = None
        self.cookie_header_data = None
        try:
            # check the cookie to see if a session has been started
            cookie = SimpleCookie(os.environ['HTTP_COOKIE'])
            self.__set_sid(cookie['sid'].value, False)
        except (CookieError, KeyError):
            # no session has been started for this user
            self.data = {}
            return

        # this flag indicates whether the session has been changed
        self.dirty = False

        # eagerly fetch the data for the active session (we'll probably need it)
        self.data = self.__retrieve_data()

    @staticmethod
    def __make_sid():
        """Returns a new session ID."""
        return hashlib.md5(os.urandom(16)).hexdigest()

    @staticmethod
    def __encode_data(d):
        """Returns a "pickled+" encoding of d.  d values of type db.Model are
        protobuf encoded before pickling to minimize CPU usage & data size."""
        # seperate protobufs so we'll know how to decode (they are just strings)
        eP = {} # for models encoded as protobufs
        eO = {} # for everything else
        for k,v in d.iteritems():
            if isinstance(v, db.Model):
                eP[k] = db.model_to_protobuf(v)
            else:
                eO[k] = v
        return pickle.dumps((eP,eO))

    @staticmethod
    def __decode_data(pdump):
        """Returns a data dictionary after decoding it from "pickled+" form."""
        eP, eO = pickle.loads(pdump)
        for k,v in eP.iteritems():
            eO[k] = db.model_from_protobuf(v)
        return eO

    def user_is_now_logged_in(self):
        """Assigns the session a new session ID (data carries over).  This helps
        nullify session fixation attacks."""
        self.__set_sid(self.__make_sid())
        self.dirty = True

    def start(self, expiration=None):
        """Starts a new session.  expiration specifies when it will expire.  If
        expiration is not specified, then COOKIE_LIFETIME will used to determine
        the expiration date."""
        # make a random ID (random.randrange() is 10x faster but less secure?)
        self.dirty = True
        self.data = {}
        if expiration:
            self.data['expiration'] = expiration
        else:
            self.data['expiration'] = datetime.datetime.now() + COOKIE_LIFETIME
        self.__set_sid(self.__make_sid())

    def terminate(self):
        """Ends the session and cleans it up."""
        self.__clear_data()
        self.sid = None
        del self.dirty
        del self.data
        del self.cookie_header

    def __set_sid(self, sid, make_cookie=True):
        """Sets the session ID, deleting the old session if one existed.  The
        session's data will remain intact (only the sesssion ID changes)."""
        if self.sid:
            self.__clear_data()
        self.sid = sid
        self.db_key = db.Key.from_path(SessionModel.kind(), sid)

        # set the cookie if requested
        if not make_cookie: return
        cookie = SimpleCookie()
        cookie["sid"] = self.sid
        cookie["sid"]["path"] = COOKIE_PATH
        ex = self.data['expiration']
        cookie["sid"]["expires"] = ex.strftime("%a, %d-%b-%Y %H:%M:%S PST")
        self.cookie_header_data = cookie.output(header='')

    def __clear_data(self):
        """Deletes this session from memcache and the datastore."""
        memcache.delete(self.key) # not really needed; it'll go away on its own
        db.delete(self.db_key)

    def __retrieve_data(self):
        """Returns the data associated with this session after retrieving it
        from memcache or the datastore.  Assumes self.sid is set."""
        pdump = memcache.get(self.sid)
        if pdump is None:
            # memcache lost it, go to the datastore
            session_model_instance = db.get(self.db_key)
            if session_model_instance:
                pdump = session_model_instance.pdump
            else:
                logging.error("can't find session data in the datastore for sid=%s" % self.sid)
                return {} # we lost it
        return self.__decode_data(pdump)

    def save(self, only_if_changed=True):
        """Saves the data associated with this session to memcache.  It also
        tries to persist it to the datastore."""
        if not self.sid:
            return # no session is active
        if only_if_changed and not self.dirty:
            return # nothing has changed

        # do the pickling ourselves b/c we need it for the datastore anyway
        pdump = self.__encode_data(self.data)
        mc_ok = memcache.set(self.sid, pdump)

        # persist the session to the datastore
        try:
            SessionModel(key_name=self.sid, pdump=pdump).put()
        except db.TransactionFailedError:
            logging.warning("unable to persist session to datastore for sid=%s" % self.sid)
        except db.CapabilityDisabledError:
            pass # nothing we can do here

        # retry the memcache set after the db op if the memcache set failed
        if not mc_ok:
            memcache.set(self.sid, pdump)

    def get_cookie_out(self):
        """Returns the cookie data to set (if any), otherwise None.  This also
        clears the cookie data (it only needs to be set once)."""
        if self.cookie_header_data:
            ret = self.cookie_header_data
            self.cookie_header_data = None
            return ret
        else:
            return None

    # Users may interact with the session through a dictionary-like interface.
    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data.__getitem__(key)

    def __setitem__(self, key, value):
        """Set a value named key on this session.  This will start this session
        if it had not already been started."""
        if not self.sid:
            self.start()
        self.data.__setitem__(key, value)
        self.dirty = True

    def __delitem__(self, key):
        if key == 'expiration':
            raise KeyError("expiration may not be removed")
        else:
            self.data.__delitem__(key)
            self.dirty = True

    def __iter__(self):
        """Returns an iterator over the keys (names) of the stored values."""
        return self.data.iterkeys()

    def __contains__(self, key):
        return self.data.__contains__(key)

    def __str__(self):
        """Returns a string representation of the session."""
        if self.sid:
            return "SID=%s %s" % (self.sid, self.data)
        else:
            return "uninitialized session"

class SessionMiddleware(object):
    """WSGI middleware that adds session support."""
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        # initialize a session for the current user
        global _current_session
        _current_session = Session()

        # create a hook for us to insert a cookie into the response headers
        def my_start_response(status, headers, exc_info=None):
            cookie_out = _current_session.get_cookie_out()
            if cookie_out:
                headers.append(('Set-Cookie', cookie_out))
            _current_session.save() # store the session if it was changed
            return start_response(status, headers, exc_info)

        # let the app do its thing
        return self.app(environ, my_start_response)