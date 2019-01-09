import random
import re
from collections import namedtuple

import mock
import pytest
from blinker import Namespace
from k8s.models.common import ObjectMeta
from requests import Response

from fiaas_deploy_daemon.crd import status
from fiaas_deploy_daemon.crd.status import _cleanup, OLD_STATUSES_TO_KEEP, LAST_UPDATED_KEY, now
from fiaas_deploy_daemon.crd.types import FiaasApplicationStatus
from fiaas_deploy_daemon.lifecycle import DEPLOY_FAILED, DEPLOY_STARTED, DEPLOY_SUCCESS, DEPLOY_INITIATED

LAST_UPDATE = now()
LOG_LINE = "This is a log line from a test."
DEPLOYMENT_ID = u"deployment_id"
NAME = u"name"
VALID_NAME = re.compile(r"^[a-z0-9.-]+$")


class TestStatusReport(object):
    @pytest.fixture
    def get_or_create(self):
        with mock.patch("fiaas_deploy_daemon.crd.status.FiaasApplicationStatus.get_or_create", spec_set=True) as m:
            yield m

    @pytest.fixture
    def find(self):
        with mock.patch("fiaas_deploy_daemon.crd.status.FiaasApplicationStatus.find", spec_set=True) as m:
            m.return_value = []
            yield m

    @pytest.fixture
    def delete(self):
        with mock.patch("fiaas_deploy_daemon.crd.status.FiaasApplicationStatus.delete", spec_set=True) as m:
            yield m

    @pytest.fixture
    def signal(self, monkeypatch):
        s = Namespace().signal
        monkeypatch.setattr("fiaas_deploy_daemon.crd.status.signal", s)
        yield s

    @pytest.fixture
    def logs(self):
        with mock.patch("fiaas_deploy_daemon.crd.status._get_logs") as m:
            m.return_value = [LOG_LINE]
            yield m

    # create vs update => new_status, url, post/put
    def pytest_generate_tests(self, metafunc):
        if metafunc.cls == self.__class__ and "test_data" in metafunc.fixturenames:
            TestData = namedtuple("TestData", ("signal_name", "action", "result", "new", "called_mock", "ignored_mock"))
            name2result = {
                DEPLOY_STARTED: u"RUNNING",
                DEPLOY_FAILED: u"FAILED",
                DEPLOY_SUCCESS: u"SUCCESS",
                DEPLOY_INITIATED: u"INITIATED"
            }
            action2data = {
                "create": (True, "post", "put"),
                "update": (False, "put", "post")
            }
            for signal_name in (DEPLOY_STARTED, DEPLOY_FAILED, DEPLOY_SUCCESS, DEPLOY_INITIATED):
                for action in ("create", "update"):
                    test_data = TestData(signal_name, action, name2result[signal_name], *action2data[action])
                    test_id = "{} status on {}".format(action, signal_name)
                    metafunc.addcall({"test_data": test_data}, test_id)

    @pytest.mark.usefixtures("post", "put", "find", "logs")
    def test_action_on_signal(self, request, get_or_create, app_spec, test_data, signal):
        app_name = '{}-isb5oqum36ylo'.format(test_data.signal_name)
        app_spec = app_spec._replace(name=test_data.signal_name)
        labels = {"app": app_spec.name, "fiaas/deployment_id": app_spec.deployment_id}
        annotations = {"fiaas/last_updated": LAST_UPDATE}
        metadata = ObjectMeta(name=app_name, namespace="default", labels=labels, annotations=annotations)
        expected_logs = [LOG_LINE]
        get_or_create.return_value = FiaasApplicationStatus(new=test_data.new, metadata=metadata,
                                                            result=test_data.result, logs=expected_logs)
        status.connect_signals()
        expected_call = {
            'apiVersion': 'fiaas.schibsted.io/v1',
            'kind': 'ApplicationStatus',
            'result': test_data.result,
            'logs': expected_logs,
            'metadata': {
                'labels': {
                    'app': app_spec.name,
                    'fiaas/deployment_id': app_spec.deployment_id
                },
                'annotations': {
                    'fiaas/last_updated': LAST_UPDATE
                },
                'namespace': 'default',
                'name': app_name,
                'ownerReferences': [],
                'finalizers': [],
            }
        }
        called_mock = request.getfixturevalue(test_data.called_mock)
        mock_response = mock.create_autospec(Response)
        mock_response.json.return_value = expected_call
        called_mock.return_value = mock_response

        with mock.patch("fiaas_deploy_daemon.crd.status.now") as mnow:
            mnow.return_value = LAST_UPDATE
            signal(test_data.signal_name).send(app_name=app_spec.name, namespace=app_spec.namespace, deployment_id=app_spec.deployment_id,
                                               repository=None)

        get_or_create.assert_called_once_with(metadata=metadata, result=test_data.result, logs=expected_logs)
        if test_data.action == "create":
            url = '/apis/fiaas.schibsted.io/v1/namespaces/default/application-statuses/'
        else:
            url = '/apis/fiaas.schibsted.io/v1/namespaces/default/application-statuses/{}'.format(app_name)
        ignored_mock = request.getfixturevalue(test_data.ignored_mock)
        called_mock.assert_called_once_with(url, expected_call)
        ignored_mock.assert_not_called()

    @pytest.mark.parametrize("deployment_id", (
            u"fiaas/fiaas-deploy-daemon:latest",
            u"1234123",
            u"The Ultimate Deployment ID",
            u"@${[]}!#%&/()=?"
    ))
    def test_create_name(self, deployment_id):
        final_name = status.create_name(NAME, deployment_id)
        assert VALID_NAME.match(final_name), "Name is not valid"

    def test_clean_up(self, app_spec, find, delete):
        returned_statuses = [_create_status(i) for i in range(20)]
        returned_statuses.append(_create_status(100, False))
        random.shuffle(returned_statuses)
        find.return_value = returned_statuses
        _cleanup(app_spec.name, app_spec.namespace)
        expected_calls = [mock.call("name-{}".format(i), "test") for i in range(20 - OLD_STATUSES_TO_KEEP)]
        expected_calls.insert(0, mock.call("name-100", "test"))
        assert delete.call_args_list == expected_calls


def _create_status(i, annotate=True):
    annotations = {LAST_UPDATED_KEY: "2020-12-12T23.59.{:02}".format(i)} if annotate else None
    metadata = ObjectMeta(name="name-{}".format(i), namespace="test", annotations=annotations)
    return FiaasApplicationStatus(new=False, metadata=metadata, result=u"SUCCESS")
