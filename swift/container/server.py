# Copyright (c) 2010 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import with_statement
import errno
import os
import socket
import time
import traceback
from urllib import unquote
from xml.sax import saxutils
from datetime import datetime

import simplejson
from eventlet.timeout import Timeout
from eventlet import TimeoutError
from webob import Request, Response
from webob.exc import HTTPAccepted, HTTPBadRequest, HTTPConflict, \
    HTTPCreated, HTTPException, HTTPInternalServerError, HTTPNoContent, \
    HTTPNotFound, HTTPPreconditionFailed, HTTPMethodNotAllowed

from swift.common import CONTAINER_LISTING_LIMIT
from swift.common.db import ContainerBroker
from swift.common.utils import get_logger, get_param, hash_path, \
    storage_directory, split_path, mkdirs
from swift.common.constraints import check_mount, check_float, \
    check_xml_encodable
from swift.common.bufferedhttp import http_connect
from swift.common.healthcheck import healthcheck
from swift.common.exceptions import ConnectionTimeout, MessageTimeout
from swift.common.db_replicator import ReplicatorRpc

DATADIR = 'containers'


class ContainerController(object):
    """WSGI Controller for the container server."""

    log_name = 'container'

    def __init__(self, conf):
        self.logger = get_logger(conf, self.log_name)
        self.root = conf.get('devices', '/srv/node/')
        self.mount_check = conf.get('mount_check', 'true').lower() in \
                              ('true', 't', '1', 'on', 'yes', 'y')
        self.node_timeout = int(conf.get('node_timeout', 3))
        self.conn_timeout = float(conf.get('conn_timeout', 0.5))
        self.replicator_rpc = ReplicatorRpc(self.root, DATADIR,
                                            ContainerBroker, self.mount_check)

    def _get_container_broker(self, drive, part, account, container):
        """
        Get a DB broker for the container.

        :param drive: drive that holds the container
        :param part: partition the container is in
        :param account: account name
        :param container: container name
        :returns: ContainerBroker object
        """
        hsh = hash_path(account, container)
        db_dir = storage_directory(DATADIR, part, hsh)
        db_path = os.path.join(self.root, drive, db_dir, hsh + '.db')
        return ContainerBroker(db_path, account=account, container=container,
                               logger=self.logger)

    def account_update(self, req, account, container, broker):
        """
        Update the account server with latest container info.

        :param req: webob.Request object
        :param account: account name
        :param container: container name
        :param borker: container DB broker object
        :returns: if the account request returns a 404 error code,
                  HTTPNotFound response object, otherwise None.
        """
        account_host = req.headers.get('X-Account-Host')
        account_partition = req.headers.get('X-Account-Partition')
        account_device = req.headers.get('X-Account-Device')
        if all([account_host, account_partition, account_device]):
            account_ip, account_port = account_host.split(':')
            new_path = '/' + '/'.join([account, container])
            info = broker.get_info()
            account_headers = {'x-put-timestamp': info['put_timestamp'],
                'x-delete-timestamp': info['delete_timestamp'],
                'x-object-count': info['object_count'],
                'x-bytes-used': info['bytes_used'],
                'x-cf-trans-id': req.headers.get('X-Cf-Trans-Id', '-')}
            if req.headers.get('x-account-override-deleted', 'no').lower() == \
                    'yes':
                account_headers['x-account-override-deleted'] = 'yes'
            try:
                with ConnectionTimeout(self.conn_timeout):
                    conn = http_connect(account_ip, account_port,
                        account_device, account_partition, 'PUT', new_path,
                        account_headers)
                with Timeout(self.node_timeout):
                    account_response = conn.getresponse()
                    account_response.read()
                    if account_response.status == 404:
                        return HTTPNotFound(request=req)
                    elif account_response.status < 200 or \
                            account_response.status > 299:
                        self.logger.error('ERROR Account update failed '
                            'with %s:%s/%s transaction %s (will retry '
                            'later): Response %s %s' % (account_ip,
                            account_port, account_device,
                            req.headers.get('x-cf-trans-id'),
                            account_response.status,
                            account_response.reason))
            except:
                self.logger.exception('ERROR account update failed with '
                    '%s:%s/%s transaction %s (will retry later)' %
                    (account_ip, account_port, account_device,
                     req.headers.get('x-cf-trans-id', '-')))
        return None

    def DELETE(self, req):
        """Handle HTTP DELETE request."""
        try:
            drive, part, account, container, obj = split_path(
                unquote(req.path), 4, 5, True)
        except ValueError, err:
            return HTTPBadRequest(body=str(err), content_type='text/plain',
                                request=req)
        if 'x-timestamp' not in req.headers or \
                    not check_float(req.headers['x-timestamp']):
            return HTTPBadRequest(body='Missing timestamp', request=req,
                        content_type='text/plain')
        if self.mount_check and not check_mount(self.root, drive):
            return Response(status='507 %s is not mounted' % drive)
        broker = self._get_container_broker(drive, part, account, container)
        if not os.path.exists(broker.db_file):
            return HTTPNotFound()
        if obj:     # delete object
            broker.delete_object(obj, req.headers.get('x-timestamp'))
            return HTTPNoContent(request=req)
        else:
            # delete container
            if not broker.empty():
                return HTTPConflict(request=req)
            existed = float(broker.get_info()['put_timestamp']) and \
                      not broker.is_deleted()
            broker.delete_db(req.headers['X-Timestamp'])
            if not broker.is_deleted():
                return HTTPConflict(request=req)
            resp = self.account_update(req, account, container, broker)
            if resp:
                return resp
            if existed:
                return HTTPNoContent(request=req)
            return HTTPAccepted(request=req)

    def PUT(self, req):
        """Handle HTTP PUT request."""
        try:
            drive, part, account, container, obj = split_path(
                unquote(req.path), 4, 5, True)
        except ValueError, err:
            return HTTPBadRequest(body=str(err), content_type='text/plain',
                                request=req)
        if 'x-timestamp' not in req.headers or \
                    not check_float(req.headers['x-timestamp']):
            return HTTPBadRequest(body='Missing timestamp', request=req,
                        content_type='text/plain')
        if self.mount_check and not check_mount(self.root, drive):
            return Response(status='507 %s is not mounted' % drive)
        broker = self._get_container_broker(drive, part, account, container)
        if obj:     # put container object
            if not os.path.exists(broker.db_file):
                return HTTPNotFound()
            broker.put_object(obj, req.headers['x-timestamp'],
                int(req.headers['x-size']), req.headers['x-content-type'],
                req.headers['x-etag'])
            return HTTPCreated(request=req)
        else:   # put container
            if not os.path.exists(broker.db_file):
                broker.initialize(req.headers['x-timestamp'])
                created = True
            else:
                created = broker.is_deleted()
                broker.update_put_timestamp(req.headers['x-timestamp'])
                if broker.is_deleted():
                    return HTTPConflict(request=req)
            resp = self.account_update(req, account, container, broker)
            if resp:
                return resp
            if created:
                return HTTPCreated(request=req)
            else:
                return HTTPAccepted(request=req)

    def HEAD(self, req):
        """Handle HTTP HEAD request."""
        try:
            drive, part, account, container, obj = split_path(
                unquote(req.path), 4, 5, True)
        except ValueError, err:
            return HTTPBadRequest(body=str(err), content_type='text/plain',
                                request=req)
        if self.mount_check and not check_mount(self.root, drive):
            return Response(status='507 %s is not mounted' % drive)
        broker = self._get_container_broker(drive, part, account, container)
        broker.pending_timeout = 0.1
        broker.stale_reads_ok = True
        if broker.is_deleted():
            return HTTPNotFound(request=req)
        info = broker.get_info()
        headers = {
            'X-Container-Object-Count': info['object_count'],
            'X-Container-Bytes-Used': info['bytes_used'],
            'X-Timestamp': info['created_at'],
            'X-PUT-Timestamp': info['put_timestamp'],
        }
        return HTTPNoContent(request=req, headers=headers)

    def GET(self, req):
        """Handle HTTP GET request."""
        try:
            drive, part, account, container, obj = split_path(
                unquote(req.path), 4, 5, True)
        except ValueError, err:
            return HTTPBadRequest(body=str(err), content_type='text/plain',
                                request=req)
        if self.mount_check and not check_mount(self.root, drive):
            return Response(status='507 %s is not mounted' % drive)
        broker = self._get_container_broker(drive, part, account, container)
        broker.pending_timeout = 0.1
        broker.stale_reads_ok = True
        if broker.is_deleted():
            return HTTPNotFound(request=req)
        info = broker.get_info()
        resp_headers = {
            'X-Container-Object-Count': info['object_count'],
            'X-Container-Bytes-Used': info['bytes_used'],
            'X-Timestamp': info['created_at'],
            'X-PUT-Timestamp': info['put_timestamp'],
        }
        try:
            path = get_param(req, 'path')
            prefix = get_param(req, 'prefix')
            delimiter = get_param(req, 'delimiter')
            if delimiter and (len(delimiter) > 1 or ord(delimiter) > 254):
                # delimiters can be made more flexible later
                return HTTPPreconditionFailed(body='Bad delimiter')
            marker = get_param(req, 'marker', '')
            limit = CONTAINER_LISTING_LIMIT
            given_limit = get_param(req, 'limit')
            if given_limit and given_limit.isdigit():
                limit = int(given_limit)
                if limit > CONTAINER_LISTING_LIMIT:
                    return HTTPPreconditionFailed(request=req,
                        body='Maximum limit is %d' % CONTAINER_LISTING_LIMIT)
            query_format = get_param(req, 'format')
        except UnicodeDecodeError, err:
            return HTTPBadRequest(body='parameters not utf8',
                                  content_type='text/plain', request=req)
        header_format = req.accept.first_match(['text/plain',
                                                'application/json',
                                                'application/xml'])
        format = query_format if query_format else header_format
        if format.startswith('application/'):
            format = format[12:]
        container_list = broker.list_objects_iter(limit, marker, prefix,
                                                  delimiter, path)
        if format == 'json':
            out_content_type = 'application/json'
            json_pattern = ['"name":%s', '"hash":"%s"', '"bytes":%s',
                            '"content_type":%s, "last_modified":"%s"']
            json_pattern = '{' + ','.join(json_pattern) + '}'
            json_out = []
            for (name, created_at, size, content_type, etag) in container_list:
                # escape name and format date here
                name = simplejson.dumps(name)
                created_at = datetime.utcfromtimestamp(
                    float(created_at)).isoformat()
                if content_type is None:
                    json_out.append('{"subdir":%s}' % name)
                else:
                    content_type = simplejson.dumps(content_type)
                    json_out.append(json_pattern % (name,
                                                    etag,
                                                    size,
                                                    content_type,
                                                    created_at))
            container_list = '[' + ','.join(json_out) + ']'
        elif format == 'xml':
            out_content_type = 'application/xml'
            xml_output = []
            for (name, created_at, size, content_type, etag) in container_list:
                # escape name and format date here
                name = saxutils.escape(name)
                created_at = datetime.utcfromtimestamp(
                    float(created_at)).isoformat()
                if content_type is None:
                    xml_output.append('<subdir name="%s" />' % name)
                else:
                    content_type = saxutils.escape(content_type)
                    xml_output.append('<object><name>%s</name><hash>%s</hash>'\
                           '<bytes>%d</bytes><content_type>%s</content_type>'\
                           '<last_modified>%s</last_modified></object>' % \
                           (name, etag, size, content_type, created_at))
            container_list = ''.join([
                '<?xml version="1.0" encoding="UTF-8"?>\n',
                '<container name=%s>' % saxutils.quoteattr(container),
                ''.join(xml_output), '</container>'])
        else:
            if not container_list:
                return HTTPNoContent(request=req, headers=resp_headers)
            out_content_type = 'text/plain'
            container_list = '\n'.join(r[0] for r in container_list) + '\n'
        ret = Response(body=container_list, request=req, headers=resp_headers)
        ret.content_type = out_content_type
        ret.charset = 'utf8'
        return ret

    def POST(self, req):
        """
        Handle HTTP POST request (json-encoded RPC calls for replication.)
        """
        try:
            post_args = split_path(unquote(req.path), 3)
        except ValueError, err:
            return HTTPBadRequest(body=str(err), content_type='text/plain',
                                request=req)
        drive, partition, hash = post_args
        if self.mount_check and not check_mount(self.root, drive):
            return Response(status='507 %s is not mounted' % drive)
        try:
            args = simplejson.load(req.body_file)
        except ValueError, err:
            return HTTPBadRequest(body=str(err), content_type='text/plain')
        ret = self.replicator_rpc.dispatch(post_args, args)
        ret.request = req
        return ret

    def __call__(self, env, start_response):
        start_time = time.time()
        req = Request(env)
        if req.path_info == '/healthcheck':
            return healthcheck(req)(env, start_response)
        elif not check_xml_encodable(req.path_info):
            res = HTTPPreconditionFailed(body='Invalid UTF8')
        else:
            try:
                if hasattr(self, req.method):
                    res = getattr(self, req.method)(req)
                else:
                    res = HTTPMethodNotAllowed()
            except:
                self.logger.exception('ERROR __call__ error with %s %s '
                    'transaction %s' % (env.get('REQUEST_METHOD', '-'),
                    env.get('PATH_INFO', '-'), env.get('HTTP_X_CF_TRANS_ID',
                    '-')))
                res = HTTPInternalServerError(body=traceback.format_exc())
        trans_time = '%.4f' % (time.time() - start_time)
        log_message = '%s - - [%s] "%s %s" %s %s "%s" "%s" "%s" %s' % (
            req.remote_addr,
            time.strftime('%d/%b/%Y:%H:%M:%S +0000',
                          time.gmtime()),
            req.method, req.path,
            res.status.split()[0], res.content_length or '-',
            req.headers.get('x-cf-trans-id', '-'),
            req.referer or '-', req.user_agent or '-',
            trans_time)
        if req.method.upper() == 'POST':
            self.logger.debug(log_message)
        else:
            self.logger.info(log_message)
        return res(env, start_response)