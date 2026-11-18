"""Offline regressions for abruptly closed model HTTPS connections."""
import unittest
from unittest.mock import patch, sentinel

import requests

from src.core.model_telemetry import _post_with_tls_recovery


class TLSRecoveryTests(unittest.TestCase):
    def call(self):
        return _post_with_tls_recovery('https://example.org/v1/chat/completions',
                                      headers={'Authorization': 'Bearer offline'},
                                      body={'model': 'review-model'}, timeout=30)

    @patch('src.core.model_telemetry.time.sleep')
    @patch('src.core.model_telemetry.requests.post')
    def test_eof_recovers_with_identical_request(self, post, sleep):
        post.side_effect = [requests.exceptions.SSLError(
            '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol'),
            sentinel.response]
        self.assertIs(self.call(), sentinel.response)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        self.assertNotIn('verify', post.call_args.kwargs)
        sleep.assert_called_once_with(1)

    @patch('src.core.model_telemetry.time.sleep')
    @patch('src.core.model_telemetry.requests.post')
    def test_persistent_eof_is_bounded(self, post, sleep):
        error = requests.exceptions.SSLError('UNEXPECTED_EOF_WHILE_READING')
        post.side_effect = error
        with self.assertRaises(requests.exceptions.SSLError) as caught:
            self.call()
        self.assertIs(caught.exception, error)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    @patch('src.core.model_telemetry.time.sleep')
    @patch('src.core.model_telemetry.requests.post')
    def test_other_failures_are_not_retried(self, post, sleep):
        for error in [requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED'),
                      requests.exceptions.SSLError('TLSV1_ALERT_PROTOCOL_VERSION'),
                      requests.exceptions.ReadTimeout('read timed out'),
                      requests.exceptions.ConnectionError('connection refused')]:
            with self.subTest(error=str(error)):
                post.reset_mock()
                post.side_effect = error
                with self.assertRaises(type(error)):
                    self.call()
                post.assert_called_once()
        sleep.assert_not_called()
