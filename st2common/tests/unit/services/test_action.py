# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import mock
import jsonschema

from st2actions.container.base import RunnerContainer
from st2common.constants import action as action_constants
from st2common.exceptions.actionrunner import ActionRunnerException
from st2common.models.db.action import LiveActionDB
from st2common.models.api.action import RunnerTypeAPI, ActionAPI
from st2common.models.system.common import ResourceReference
from st2common.persistence.action import RunnerType, Action
from st2common.services import action as action_service
from st2common.transport.publishers import PoolPublisher
from st2common.util import isotime
from st2common.util import action_db
from st2tests import DbTestCase


RUNNER = {
    'name': 'local-shell-script',
    'description': 'A runner to execute local command.',
    'enabled': True,
    'runner_parameters': {
        'hosts': {'type': 'string'},
        'cmd': {'type': 'string'}
    },
    'runner_module': 'st2actions.runners.fabricrunner'
}

ACTION = {
    'name': 'my.action',
    'description': 'my test',
    'enabled': True,
    'entry_point': '/tmp/test/action.sh',
    'pack': 'default',
    'runner_type': 'local-shell-script',
    'parameters': {
        'a': {
            'type': 'string',
            'default': 'abc'
        }
    },
    'notify': {
        'on_complete': {
            'message': 'My awesome action is complete. Party time!!!',
            'channels': ['notify.slack']
        }
    }
}

ACTION_REF = ResourceReference(name='my.action', pack='default').ref
USERNAME = 'stanley'


@mock.patch.object(PoolPublisher, 'publish', mock.MagicMock())
class TestActionExecutionService(DbTestCase):

    @classmethod
    def setUpClass(cls):
        super(TestActionExecutionService, cls).setUpClass()
        cls.runner = RunnerTypeAPI(**RUNNER)
        cls.runnerdb = RunnerType.add_or_update(RunnerTypeAPI.to_model(cls.runner))
        cls.action = ActionAPI(**ACTION)
        cls.actiondb = Action.add_or_update(ActionAPI.to_model(cls.action))
        cls.container = RunnerContainer()

    @classmethod
    def tearDownClass(cls):
        Action.delete(cls.actiondb)
        RunnerType.delete(cls.runnerdb)
        super(TestActionExecutionService, cls).tearDownClass()

    def _schedule(self):
        context = {'user': USERNAME}
        parameters = {'hosts': 'localhost', 'cmd': 'uname -a'}
        request = LiveActionDB(action=ACTION_REF, context=context, parameters=parameters)
        request, _ = action_service.schedule(request)
        execution = action_db.get_liveaction_by_id(str(request.id))
        return request, execution

    def test_schedule(self):
        request, execution = self._schedule()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.id, request.id)
        self.assertEqual(execution.action, '.'.join([self.actiondb.pack, self.actiondb.name]))
        self.assertEqual(execution.context['user'], request.context['user'])
        self.assertDictEqual(execution.parameters, request.parameters)
        self.assertEqual(execution.status, action_constants.LIVEACTION_STATUS_SCHEDULED)
        self.assertTrue(execution.notify is not None)
        # mongoengine DateTimeField stores datetime only up to milliseconds
        self.assertEqual(isotime.format(execution.start_timestamp, usec=False),
                         isotime.format(request.start_timestamp, usec=False))

    def test_schedule_invalid_parameters(self):
        parameters = {'hosts': 'localhost', 'cmd': 'uname -a', 'a': 123}
        liveaction = LiveActionDB(action=ACTION_REF, parameters=parameters)
        self.assertRaises(jsonschema.ValidationError, action_service.schedule, liveaction)

    def test_schedule_nonexistent_action(self):
        parameters = {'hosts': 'localhost', 'cmd': 'uname -a'}
        action_ref = ResourceReference(name='i.action', pack='default').ref
        execution = LiveActionDB(action=action_ref, parameters=parameters)
        self.assertRaises(ValueError, action_service.schedule, execution)

    def test_schedule_disabled_action(self):
        self.actiondb.enabled = False
        Action.add_or_update(self.actiondb)
        parameters = {'hosts': 'localhost', 'cmd': 'uname -a'}
        execution = LiveActionDB(action=ACTION_REF, parameters=parameters)
        self.assertRaises(ValueError, action_service.schedule, execution)
        self.actiondb.enabled = True
        Action.add_or_update(self.actiondb)

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(return_value={'key': 'value'}))
    def test_execute(self):
        request, execution = self._schedule()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.id, request.id)
        self.assertEqual(execution.status, action_constants.LIVEACTION_STATUS_SCHEDULED)
        action_service.execute(execution, self.container)
        updated_execution = action_db.get_liveaction_by_id(str(request.id))
        self.assertEqual(updated_execution.status, action_constants.LIVEACTION_STATUS_RUNNING)

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(side_effect=Exception('Boom!')))
    def test_execute_failure(self):
        request, execution = self._schedule()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.id, request.id)
        self.assertEqual(execution.status, action_constants.LIVEACTION_STATUS_SCHEDULED)
        self.assertRaises(Exception, action_service.execute, execution, self.container)
        updated_execution = action_db.get_liveaction_by_id(str(request.id))
        self.assertEqual(updated_execution.status, action_constants.LIVEACTION_STATUS_FAILED)

    @mock.patch.object(RunnerContainer, 'dispatch', mock.MagicMock(return_value=None))
    def test_execute_no_result(self):
        request, execution = self._schedule()
        self.assertIsNotNone(execution)
        self.assertEqual(execution.id, request.id)
        self.assertEqual(execution.status, action_constants.LIVEACTION_STATUS_SCHEDULED)
        self.assertRaises(ActionRunnerException, action_service.execute, execution, self.container)
        updated_execution = action_db.get_liveaction_by_id(str(request.id))
        self.assertEqual(updated_execution.status, action_constants.LIVEACTION_STATUS_FAILED)
