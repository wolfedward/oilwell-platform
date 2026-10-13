import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from tests import test_monitoring_session as sessions


class MonitoringHttpTests(unittest.TestCase):
    setUp = sessions.MonitoringSessionTests.setUp
    close = sessions.MonitoringSessionTests.close
    request = sessions.MonitoringSessionTests.request
    login = sessions.MonitoringSessionTests.login

    def test_authenticated_read_is_routed_and_permission_checked_before_query(self):
        from backend.monitoring.runtime import MonitoringRuntime
        query = Mock()
        query.read.return_value = {'context': {'environment': 'test'}, 'items': []}
        self.server.monitoring_runtime = MonitoringRuntime('ready', query=query)
        path = f'/api/production-monitoring/wells/{uuid4()}/latest'
        self.assertEqual(self.request('GET', path)[0], 401)
        query.read.assert_not_called()
        cookie, _ = self.login()
        status, body, _ = self.request('GET', path, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body['data']['context']['environment'], 'test')
        self.assertEqual(query.read.call_count, 1)
        self.assertEqual(self.request('GET', path+'?page=1&page=2', cookie=cookie)[0], 422)
        self.assertEqual(query.read.call_count, 1)

    def test_csv_bytes_headers_and_post_are_not_a_write_path(self):
        import http.client
        from backend.monitoring.runtime import MonitoringRuntime
        from backend.monitoring.contracts import CsvExport
        query = Mock()
        query.export.return_value = CsvExport('wells.csv', b'id\r\n1\r\n')
        self.server.monitoring_runtime = MonitoringRuntime('ready', query=query)
        cookie, _ = self.login()
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('GET', '/api/production-monitoring/wells.csv?revision=1&query_total=1&received_before=2026-01-01T00:00:00Z',
                     headers={'Cookie': cookie})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertIn('wells.csv', response.getheader('Content-Disposition'))
        self.assertEqual(response.read(), b'id\r\n1\r\n')
        conn.close()
        self.assertEqual(self.request('POST', '/api/production-monitoring/wells', {}, cookie=cookie,
                                     headers={'Origin': self.origin})[0], 404)


class MonitoringApiTests(unittest.TestCase):
    def test_disabled_monitoring_keeps_optional_pg_driver_optional(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, '-B', '-c',
            "import sys; sys.modules['psycopg'] = None; import backend.http_server"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_routes_permissions_and_disabled_no_fallback(self):
        try: from backend.monitoring.api import MonitoringApi
        except ImportError: self.fail('Monitoring API is not implemented')
        from backend.monitoring.runtime import MonitoringRuntime
        query = Mock()
        query.read.return_value = {'items': []}
        handler = Mock()
        handler.server.monitoring_runtime = MonitoringRuntime('ready', query=query)
        handler._require.return_value = SimpleNamespace(permissions=frozenset({'registry.read', 'telemetry.read'}))
        path = f'/api/production-monitoring/wells/{uuid4()}/latest'
        self.assertTrue(MonitoringApi(handler).get(path))
        self.assertEqual(query.read.call_args.args[0], 'latest')
        self.assertEqual(handler._send_json.call_args.args[0], 200)
        handler._require.return_value = None
        query.reset_mock()
        MonitoringApi(handler).get(path)
        query.read.assert_not_called()
        handler._require.return_value = SimpleNamespace(permissions=frozenset({'registry.read'}))
        MonitoringApi(handler).get(path)
        self.assertEqual(handler._send_json.call_args.args[0], 403)
        query.read.assert_not_called()
        handler._require.return_value = SimpleNamespace(permissions=frozenset({'registry.read', 'telemetry.read'}))
        handler.server.monitoring_runtime = MonitoringRuntime('disabled', code='monitoring.disabled')
        MonitoringApi(handler).get(path)
        self.assertEqual(handler._send_json.call_args.args[0], 503)
        self.assertFalse(MonitoringApi(handler).get('/api/registry/wells'))

    def test_export_boundary_10000_and_10mib(self):
        try: from backend.monitoring.exports import make_csv
        except ImportError: self.fail('Monitoring CSV is not implemented')
        from backend.monitoring.errors import MonitoringError
        self.assertEqual(make_csv('x.csv', ['id'], [{'id': i} for i in range(10000)]).body.count(b'\n'), 10001)
        for rows in ([{'id': i} for i in range(10001)], [{'id': 'a' * (10 * 1024 * 1024)}]):
            with self.assertRaises(MonitoringError) as caught: make_csv('x.csv', ['id'], rows)
            self.assertEqual(caught.exception.status, 422)

    def test_csv_formula_and_unicode(self):
        try: from backend.monitoring.exports import make_csv
        except ImportError: self.fail('Monitoring CSV is not implemented')
        import csv, io
        values = ['=1+2', '+SUM(A1)', '-text', '@formula', '\t=x', '\n=x', ' 中文,\n第二行']
        data = make_csv('x.csv', ['name', 'value'], [{'name': value, 'value': -12.123456789} for value in values])
        rows = list(csv.DictReader(io.StringIO(data.body.decode('utf-8-sig'))))
        self.assertTrue(all(x['name'].startswith("'") for x in rows[:6]))
        self.assertEqual(rows[-1]['name'], values[-1])
        self.assertTrue(all(x['value'] == '-12.123456789' for x in rows))

    def test_timezone_and_large_integer(self):
        from backend.monitoring.contracts import validate_filters
        value = validate_filters('history', {'well_id': str(uuid4()), 'registry_device_id': str(uuid4()), 'point_code': 'load',
            'from_time': '2026-09-01T08:00:00+08:00', 'to_time': '2026-10-02T00:00:00Z', 'revision': '9007199254740993'})
        self.assertEqual(value['from_time'].isoformat(), '2026-09-01T00:00:00+00:00')
        self.assertEqual(value['revision'], '9007199254740993')
