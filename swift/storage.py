import mimetypes
import os
import posixpath
import re
from datetime import datetime
from functools import wraps
from io import BytesIO
from time import time
import magic
from django.conf import settings
from django.contrib.staticfiles.storage import CachedFilesMixin, ManifestFilesMixin
from django.core.exceptions import ImproperlyConfigured
from django.core.files import File
from django.core.files.storage import Storage
from six.moves.urllib import parse as urlparse

try:
    from django.utils.deconstruct import deconstructible
except ImportError:
    def deconstructible(arg):
        return arg

try:
    import swiftclient
    from swiftclient.utils import generate_temp_url
except ImportError:
    raise ImproperlyConfigured("Could not load swiftclient library")


def setting(name, default='__not_set__'):
    try:
        return getattr(settings, name)
    except AttributeError:
        if default != '__not_set__':
            return default
        raise ImproperlyConfigured('The {} setting is required'.format(name))


def ensure_setup(func):
    def inner(self, *args):
        if self.storage_url is None:
            self._setup()
        return func(self, *args)
    return inner



def safe_normpath(path):
    """
    Avoid doing normpath on empty path since:
    - posixpath.normpath('') -> '.'
    - we should avoid relative paths with swift
    """
    if path:
        return posixpath.normpath(path)
    return path


def validate_settings(backend):
    # Check mandatory parameters
    if not backend.api_auth_url:
        raise ImproperlyConfigured("The SWIFT_AUTH_URL setting is required")

    if not backend.api_username:
        raise ImproperlyConfigured("The SWIFT_USERNAME setting is required")

    if not backend.api_key:
        raise ImproperlyConfigured("The SWIFT_KEY or SWIFT_PASSWORD setting is required")

    if not backend.container_name:
        raise ImproperlyConfigured("No container name defined. Use SWIFT_CONTAINER_NAME \
        or SWIFT_STATIC_CONTAINER_NAME depending on the backend")

    # Detect auth version if not defined
    # http://docs.openstack.org/developer/python-swiftclient/cli.html#authentication
    if not backend.auth_version:
        if (backend.user_domain_name or backend.user_domain_id) and \
           (backend.project_domain_name or backend.project_domain_id):
            # Set version 3 if domain and project scoping is defined
            backend.auth_version = '3'
        else:
            if backend.tenant_name or backend.tenant_id:
                # Set version 2 if a tenant is defined
                backend.auth_version = '2'
            else:
                # Set version 1 if no tenant is not defined
                backend.auth_version = '1'

    # Enforce auth_version into a string (more future proof)
    backend.auth_version = str(backend.auth_version)

    # Validate v2 auth parameters
    if backend.auth_version == '2':
        if not (backend.tenant_name or backend.tenant_id):
            raise ImproperlyConfigured("SWIFT_TENANT_ID or SWIFT_TENANT_NAME must \
             be defined when using version 2 auth")

    # Validate v3 auth parameters
    if backend.auth_version == '3':
        if not (backend.user_domain_name or backend.user_domain_id):
            raise ImproperlyConfigured("SWIFT_USER_DOMAIN_NAME or \
            SWIFT_USER_DOMAIN_ID must be defined when using version 3 auth")

        if not (backend.project_domain_name or backend.project_domain_id):
            raise ImproperlyConfigured("SWIFT_PROJECT_DOMAIN_NAME or \
            SWIFT_PROJECT_DOMAIN_ID must be defined when using version 3 auth")

        if not (backend.tenant_name or backend.tenant_id):
            raise ImproperlyConfigured("SWIFT_PROJECT_ID or SWIFT_PROJECT_NAME must \
             be defined when using version 3 auth")

    # Validate temp_url parameters
    if backend.use_temp_urls:
        if backend.temp_url_key is None:
            raise ImproperlyConfigured("SWIFT_TEMP_URL_KEY must be set when \
             SWIFT_USE_TEMP_URL is True")

        # Encode temp_url_key as bytes
        try:
            backend.temp_url_key = backend.temp_url_key.encode('ascii')
        except UnicodeEncodeError:
            raise ImproperlyConfigured("SWIFT_TEMP_URL_KEY must ascii")

    # Misc sanity checks
    if not isinstance(backend.os_extra_options, dict):
        raise ImproperlyConfigured("SWIFT_EXTRA_OPTIONS must be a dict")


def prepend_name_prefix(func):
    """
    Decorator that wraps instance methods to prepend the instance's filename
    prefix to the beginning of the referenced filename. Must only be used on
    instance methods where the first parameter after `self` is `name` or a
    comparable parameter of a different name.
    """
    @wraps(func)
    def prepend_prefix(self, name, *args, **kwargs):
        name = self.name_prefix + safe_normpath(name)
        return func(self, name, *args, **kwargs)
    return prepend_prefix


@deconstructible
class SwiftStorage(Storage):
    api_auth_url = setting('SWIFT_AUTH_URL')
    api_username = setting('SWIFT_USERNAME')
    api_key = setting('SWIFT_KEY') or setting('SWIFT_PASSWORD')
    auth_version = setting('SWIFT_AUTH_VERSION')
    tenant_name = setting('SWIFT_TENANT_NAME') or setting('SWIFT_PROJECT_NAME')
    tenant_id = setting('SWIFT_TENANT_ID') or setting('SWIFT_PROJECT_ID')
    user_domain_name = setting('SWIFT_USER_DOMAIN_NAME')
    user_domain_id = setting('SWIFT_USER_DOMAIN_ID')
    project_domain_name = setting('SWIFT_PROJECT_DOMAIN_NAME')
    project_domain_id = setting('SWIFT_PROJECT_DOMAIN_ID')
    region_name = setting('SWIFT_REGION_NAME')
    container_name = setting('SWIFT_CONTAINER_NAME')
    auto_create_container = setting('SWIFT_AUTO_CREATE_CONTAINER', False)
    auto_create_container_public = setting(
        'SWIFT_AUTO_CREATE_CONTAINER_PUBLIC', False)
    auto_create_container_allow_orgin = setting(
        'SWIFT_AUTO_CREATE_CONTAINER_ALLOW_ORIGIN')
    auto_base_url = setting('SWIFT_AUTO_BASE_URL', True)
    override_base_url = setting('SWIFT_BASE_URL', None)
    use_temp_urls = setting('SWIFT_USE_TEMP_URLS', False)
    temp_url_key = setting('SWIFT_TEMP_URL_KEY', None)
    temp_url_duration = setting('SWIFT_TEMP_URL_DURATION', 30 * 60)
    auth_token_duration = setting('SWIFT_AUTH_TOKEN_DURATION', 60 * 60 * 23)
    os_extra_options = setting('SWIFT_EXTRA_OPTIONS', {})
    auto_overwrite = setting('SWIFT_AUTO_OVERWRITE', False)
    content_type_from_fd = setting('SWIFT_CONTENT_TYPE_FROM_FD', False)
    _token_creation_time = 0
    _token = ''
    name_prefix = setting('SWIFT_NAME_PREFIX', '')
    full_listing = setting('SWIFT_FULL_LISTING', True)
    max_retries = setting('SWIFT_MAX_RETRIES', 5)

    def __init__(self, **settings):
        # check if some of the settings provided as class attributes
        # should be overwritten
        for name, value in settings.items():
            if hasattr(self, name):
                setattr(self, name, value)

        validate_settings(self)

        self.last_headers_name = None
        self.last_headers_value = None

        # Initialize empty, meaning that this instance is not setup yet. See
        # `_setup` below
        self.storage_url = None

    def _setup(self):
        """
        Separate setup method for `storage_url` and `token` initialization.

        By separating this out of `__init__`, it becomes possible to use
        SwiftStorage as the non-default storage, without calling the storage on
        each django bootstrap. (Which is very annoying in development, as
        every dev-server reboot first then needs to connect to storage storage.)

        Each method that uses any of `token`, `storage_url` or `base_url` should
        be decorated with `ensure_setup`.
        """
        self.os_options = {
            'tenant_id': self.tenant_id,
            'tenant_name': self.tenant_name,
            'user_domain_id': self.user_domain_id,
            'user_domain_name': self.user_domain_name,
            'project_domain_id': self.project_domain_id,
            'project_domain_name': self.project_domain_name,
            'region_name': self.region_name,
        }
        self.os_options.update(self.os_extra_options)

        # Get authentication token
        self.storage_url, self.token = swiftclient.get_auth(
            self.api_auth_url,
            self.api_username,
            self.api_key,
            auth_version=self.auth_version,
            os_options=self.os_options)
        self.http_conn = swiftclient.http_connection(self.storage_url)

        # Check container
        try:
            self.swift_conn.head_container(self.container_name)
        except swiftclient.ClientException:
            headers = {}
            if self.auto_create_container:
                if self.auto_create_container_public:
                    headers['X-Container-Read'] = '.r:*'
                if self.auto_create_container_allow_orgin:
                    headers['X-Container-Meta-Access-Control-Allow-Origin'] = \
                        self.auto_create_container_allow_orgin
                self.swift_conn.put_container(self.container_name,
                                              headers=headers)
            else:
                raise ImproperlyConfigured(
                    "Container %s does not exist." % self.container_name)

        if self.auto_base_url:
            # Derive a base URL based on the authentication information from
            # the server, optionally overriding the protocol, host/port and
            # potentially adding a path fragment before the auth information.
            self.base_url = self.swift_conn.url + '/'
            if self.override_base_url is not None:
                # override the protocol and host, append any path fragments
                split_derived = urlparse.urlsplit(self.base_url)
                split_override = urlparse.urlsplit(self.override_base_url)
                split_result = [''] * 5
                split_result[0:2] = split_override[0:2]
                split_result[2] = (split_override[2] + split_derived[2]
                                   ).replace('//', '/')
                self.base_url = urlparse.urlunsplit(split_result)

            self.base_url = urlparse.urljoin(self.base_url,
                                             self.container_name)
            self.base_url += '/'
        else:
            self.base_url = self.override_base_url

    def get_token(self):
        if time() - self._token_creation_time >= self.auth_token_duration:
            new_token = swiftclient.get_auth(
                self.api_auth_url,
                self.api_username,
                self.api_key,
                auth_version=self.auth_version,
                os_options=self.os_options)[1]
            self.token = new_token
        return self._token

    def set_token(self, new_token):
        self._token_creation_time = time()
        self._token = new_token

    token = property(get_token, set_token)

    @ensure_setup
    def _open(self, name, mode='rb'):
        original_name = name
        name = self.name_prefix + safe_normpath(name)

        headers, content = self.swift_conn.get_object(self.container_name, name)
        buf = BytesIO(content)
        buf.name = os.path.basename(original_name)
        buf.mode = mode
        return File(buf)

    @ensure_setup
    def _save(self, name, content, headers=None):
        original_name = name
        # File may have already be read, always seek to the beginning
        content.seek(0)
        name = self.name_prefix + safe_normpath(name)

        if self.content_type_from_fd:
            content_type = magic.from_buffer(content.read(1024), mime=True)
            # Go back to the beginning of the file
            content.seek(0)
        else:
            content_type = mimetypes.guess_type(name)[0]
        content_length = content.size
        self.swift_conn.put_object(self.container_name,
                                   name,
                                   content,
                                   content_length=content_length,
                                   content_type=content_type,
                                   headers=headers)
        return original_name

    @ensure_setup
    def get_headers(self, name):
        """
        Optimization : only fetch headers once when several calls are made
        requiring information for the same name.
        When the caller is collectstatic, this makes a huge difference.
        According to my test, we get a *2 speed up. Which makes sense : two
        api calls were made..
        """
        if name != self.last_headers_name:
            # miss -> update
            self.last_headers_value = self.swift_conn.head_object(
                self.container_name, name)
            self.last_headers_name = name
        return self.last_headers_value

    @prepend_name_prefix
    @ensure_setup
    def exists(self, name):
        try:
            self.get_headers(name)
        except swiftclient.ClientException:
            return False
        return True

    @prepend_name_prefix
    @ensure_setup
    def delete(self, name):
        try:
            self.swift_conn.delete_object(self.container_name, name)
        except swiftclient.ClientException:
            pass

    def get_valid_name(self, name):
        s = name.strip().replace(' ', '_')
        return re.sub(r'(?u)[^-_\w./]', '', s)

    @prepend_name_prefix
    def get_available_name(self, name, max_length=None):
        """
        Returns a filename that's free on the target storage system, and
        available for new content to be written to.
        """
        if not self.auto_overwrite:
            if max_length is None:
                name = super(SwiftStorage, self).get_available_name(name)
            else:
                name = super(SwiftStorage, self).get_available_name(
                    name, max_length)

        if self.name_prefix:
            # Split out the name prefix so we can just return the bit of
            # the name that's relevant upstream, since the prefix will
            # be automatically added on subsequent requests anyway.
            empty, prefix, final = name.partition(self.name_prefix)
            return final
        else:
            return name

    @prepend_name_prefix
    def size(self, name):
        return int(self.get_headers(name)['content-length'])

    @prepend_name_prefix
    def modified_time(self, name):
        return datetime.fromtimestamp(
            float(self.get_headers(name)['x-timestamp']))

    @prepend_name_prefix
    def url(self, name):
        return self._path(name)

    @ensure_setup
    def _path(self, name):
        try:
            name = name.encode('utf-8')
        except UnicodeDecodeError:
            pass
        url = urlparse.urljoin(self.base_url, urlparse.quote(name))

        # Are we building a temporary url?
        if self.use_temp_urls:
            expires = int(time() + int(self.temp_url_duration))
            path = urlparse.unquote(urlparse.urlsplit(url).path)
            tmp_path = generate_temp_url(path, expires, self.temp_url_key, 'GET', absolute=True)
            url = urlparse.urljoin(self.base_url, tmp_path)

        return url

    def path(self, name):
        raise NotImplementedError

    @prepend_name_prefix
    def isdir(self, name):
        return '.' not in name

    @prepend_name_prefix
    @ensure_setup
    def listdir(self, path):
        container = self.swift_conn.get_container(
            self.container_name, prefix=path, full_listing=self.full_listing)
        files = []
        dirs = []
        for obj in container[1]:
            remaining_path = obj['name'][len(path):].split('/')
            key = remaining_path[0] if remaining_path[0] else remaining_path[1]

            if not self.isdir(key):
                files.append(key)
            elif key not in dirs:
                dirs.append(key)

        return dirs, files

    @prepend_name_prefix
    @ensure_setup
    def makedirs(self, dirs):
        self.swift_conn.put_object(self.container_name,
                                   '%s/.' % (self.name_prefix + dirs),
                                   contents='')

    @prepend_name_prefix
    @ensure_setup
    def rmtree(self, abs_path):
        container = self.swift_conn.get_container(self.container_name)

        for obj in container[1]:
            if obj['name'].startswith(abs_path):
                self.swift_conn.delete_object(self.container_name,
                                              obj['name'])


class StaticSwiftStorage(SwiftStorage):
    container_name = setting('SWIFT_STATIC_CONTAINER_NAME', '')
    name_prefix = setting('SWIFT_STATIC_NAME_PREFIX', '')
    auto_base_url = setting('SWIFT_STATIC_AUTO_BASE_URL', True)
    auto_create_container_public = True
    use_temp_urls = False
    override_base_url = setting('SWIFT_STATIC_BASE_URL', '')

    def get_available_name(self, name, max_length=None):
        """
        When running collectstatic we don't want to return an available name,
        we want to return the same name because if the file exists we want to
        overwrite it.
        """
        return name


class SwiftHashedFilesMixin(object):
    def file_hash(self, name, content=None):
        # Hash must be performed on the whole file content: always seek to 0.
        if content is not None and content.seekable:
            content.seek(0)

        return super(SwiftHashedFilesMixin, self).file_hash(name, content)


class CachedStaticSwiftStorage(SwiftHashedFilesMixin, CachedFilesMixin, StaticSwiftStorage):
    """
    A static file system storage backend which also saves
    hashed copies of the files it saves.
    """
    pass


class ManifestStaticSwiftStorage(SwiftHashedFilesMixin, ManifestFilesMixin, StaticSwiftStorage):
    """
    A static file system storage backend which also saves
    hashed copies of the files it saves.
    """
    def read_manifest(self):
        try:
            super(ManifestStaticSwiftStorage, self).read_manifest()
        except swiftclient.ClientException:
            return None
