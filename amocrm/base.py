# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
import json
from abc import *
from collections import defaultdict
from time import time
from copy import copy
import logging
import six

import requests

from .settings import settings
from .decorators import (amo_request, lazy_dict_property,
                         to_amo_obj, lazy_property)
from .exceptions import *

logger = logging.getLogger('amocrm')

__all__ = []

_AMO_LOGIN_PATH = '/private/api/auth.php'
_REQUEST_PARAMS = {
    'headers': {'User-Agent': 'Amocrm API module. Python powered'},
    'timeout': 3,
}
_G, _P = 'get', 'post'

_tree = lambda: defaultdict(_tree)
_session = requests.Session()


class _BaseAmoManager(six.with_metaclass(ABCMeta)):
    __slots__ = []
    format_ = 'json'

    _object_type = None
    _base_path = '/private/api/v2/%(format)s%(name)s%(path)s'
    _methods = {
        'account_info': {
            'path': 'current',
            'result': ['account'],
            'name': 'accounts',
        },
        'list': {
            'path': 'list',
            'result': True,
        },
        'add': {
            'path': 'set',
            'method': _P,
            'result': ['add', 0, 'id'],
            'container': ['add'],
        },
        'update': {
            'path': 'set',
            'method': _P,
            'container': ['update'],
            'result': ['update', 0, 'id'],
            'timestamp': True,
        },
    }
    _amo_model_class = None
    _main_field = None
    _container_name = None

    def __init__(self, user_login=None, user_hash=None,
                 domain=None, responsible_user=None):
        self._session = _session
        if user_login is not None:
            settings.set(user_login, user_hash, domain,
                         responsible_user)

    def auth(self):
        try:
            self._make_request(_AMO_LOGIN_PATH, _P, data=self._login_data)
        except AmoResponseException as e:
            raise AmoAuthException(e.resp)

    @property
    def is_auth(self):
        return 'session_id' in self._session.cookies

    @property
    def _domain(self):
        return settings.domain

    @property
    def _login_data(self):
        if settings.user_login:
            return {'USER_LOGIN': settings.user_login,
                    'USER_HASH': settings.user_hash}

    @lazy_property
    def _responsible_user(self):
        return settings.responsible_user or settings.user_login

    @classmethod
    @abstractproperty
    def name(cls):
        pass

    @property
    def container_name(self):
        return self._container_name or self.name

    def _convert_to_obj(self, result):
        if result:
            if not self._amo_model_class:
                return result
            if isinstance(result, (tuple, list)):
                return [self._amo_model_class(obj, _loaded=True) for obj in result]
            return self._amo_model_class(result, _loaded=True)

    def create(self, **kwargs):
        obj = self._amo_model_class(kwargs)
        obj.save()
        return obj

    @lazy_dict_property
    def _custom_fields(self):
        return self.get_custom_fields(to=self.name)

    @lazy_dict_property
    def account_info(self):
        return self.get_account_info()

    @lazy_property
    def rui(self):
        if isinstance(self._responsible_user, int):
            return self._responsible_user
        else:
            filter_func = lambda _: self._responsible_user == _.get('login') or \
                                    self._responsible_user == _.get('name')
            user = list(filter(filter_func, self.account_info.get('users', []))).pop()
            if user is None:
                raise Exception('Can not get responsible user id')
            return user.get('id')

    @lazy_property
    def leads_statuses(self):
        return {item.pop('name'): item for item in self.account_info.get('leads_statuses')}

    @lazy_property
    def note_types(self):
        return {item.pop('code'): item for item in self.account_info.get('note_types')}

    @lazy_property
    def task_types(self):
        return {item.pop('name'): item for item in self.account_info.get('task_types')}  # 'LETTER', 'MEETING', 'CALL'

    @lazy_property
    def users(self):
        return {item['name']: item for item in self.account_info.get('users')}

    def _make_request(self, path, method, data, headers=None):
        headers = headers or {}

        _req_params = copy(_REQUEST_PARAMS)
        headers.update(_req_params.pop('headers'))

        method = method.lower()
        params = {'type': 'json'}
        if method != _P:
            params.update(data)
            data = None

        logger.info('%s - Sending %s request to %s' % (self.__class__.name,
                                                       method, path))
        logger.debug('Data: %s \n Params: %s' % (data, params))

        resp = self._session.request(method, self._url(path), data=json.dumps(data), params=params,
                                     headers=headers, **_req_params)
        logger.debug('Url: %s', resp.url)
        if not resp.ok:
            logger.error('Something went wrong')
            raise AmoResponseException(resp)
        try:
            return resp.json()
        except ValueError:
            return resp.content

    def _create_container(self, container, data):
        name = self.container_name
        container = ['request', name] + container
        _container = _ = _tree()
        for i, elem in enumerate(container):
            if i + 1 == len(container):
                _[elem] = data
                continue
            _ = _[elem]
        return _container

    def _modify_response(self, response, result):
        if isinstance(result, (list, tuple)):
            result = ['response', self.container_name] + result
            for key in result:
                try:
                    response = response[key]
                except (TypeError, KeyError, IndexError):
                    pass
        elif result:
            try:
                response = response['response'][self.container_name]
            except (TypeError, KeyError):
                pass
        return response

    def _request(self, method, data=None, headers=None):
        if not self.is_auth:
            self.auth()
        path = self._get_path(method)
        method = self._methods[method]
        method_type = method.get('method', _G)
        timestamp, container, result = method.get('timestamp'), method.get('container'), method.get('result')

        if timestamp:
            _time = timestamp if isinstance(timestamp, str) else 'last_modified'
            data[0].setdefault(_time, int(time()))
        if container is not None:
            data = self._create_container(container, data)
        response = self._make_request(path=path, method=method_type, data=data, headers=headers)
        return self._modify_response(response, result)

    def _get_path(self, method_name):
        name = self._methods[method_name].get('name', self.name)
        path = self._methods[method_name]['path']
        if not name.startswith('/'):
            name = '/' + name
        if not name.endswith('/') and not path.startswith('/'):
            name += '/'
        return self._base_path % {'path': path, 'format': self.format_, 'name': name}

    def _url(self, path):
        return 'https://%(domain)s.amocrm.ru%(path)s' % {'domain': self._domain, 'path': path}

    def get_custom_fields(self, to):
        custom_fields = self.account_info['custom_fields'].get(to, [])
        return {field['name']: field for field in custom_fields}

    @amo_request(method='account_info')
    def get_account_info(self):
        return {}

    @to_amo_obj
    @amo_request(method='list')
    def all(self, limit=100, limit_offset=None, query=None, modified_since=None):
        request = query or {}
        if self._object_type:
            request.update({'type': self._object_type})
        if limit is not None:
            request['limit_rows'] = limit
        if limit_offset is not None:
            request['limit_offset'] = limit_offset
        return request

    def get(self, id):
        # TODO: refactor function signature, ..get(id=1)
        results = self.all(limit=1, query={'id': id, 'type': self.container_name[:-1]})
        if results is None:
            raise ValueError('Object with id %s not founded' % id)
        return results.pop()

    def search(self, query, modified_since=None):
        query = {'query': query}
        results = self.all(limit=1, query=query, modified_since=modified_since)
        return list(results).pop() if results is not None else None

    @amo_request('add')
    def add(self, **kwargs):
        return [self._add_data(**kwargs)]

    @amo_request('update')
    def update(self, **kwargs):
        return [self._update_data(**kwargs)]

    def create_or_update(self, **kwargs):
        return self._create_or_update_data(**kwargs)

    @abstractmethod
    def _add_data(self, **kwargs):
        return kwargs

    @abstractmethod
    def _update_data(self, **kwargs):
        return kwargs

    @abstractmethod
    def _create_or_update_data(self, on_field=None, **data):
        query = data.get(on_field or self._main_field)
        obj = self.search(query) if query else {}
        if obj:
            if any(obj.get(key) != data[key] for key in data.keys()):
                for key, value in data.items():
                    setattr(obj, key, value)
                obj.save()
            return obj['id']
        else:
            return self.add(**data)


class _BlankMixin(object):
    def _add_data(self, **kwargs):
        return super(_BlankMixin, self)._add_data(**kwargs)

    def _update_data(self, **kwargs):
        return super(_BlankMixin, self)._update_data(**kwargs)

    def _create_or_update_data(self, **kwargs):
        return super(_BlankMixin, self)._create_or_update_data(**kwargs)


def _Helper(_class, name):
    class Mixin(object):
        def __init__(self, *args, **kwargs):
            super(Mixin, self).__init__(*args, **kwargs)
            setattr(self, name, _class(*args, **kwargs))
            setattr(getattr(self, name), '_account_info', self.account_info)

    return Mixin
