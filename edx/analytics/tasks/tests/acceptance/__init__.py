import boto
import hashlib
import json
import logging
from luigi.s3 import S3Client
import os
import shutil
import unittest

from edx.analytics.tasks.tests.acceptance.services import fs, db, task, hive, vertica
from edx.analytics.tasks.url import url_path_join, get_target_from_url


log = logging.getLogger(__name__)

# Decorators for tagging tests


def when_s3_available(function):
    s3_available = getattr(when_s3_available, 's3_available', None)
    if s3_available is None:
        try:
            connection = boto.connect_s3()
            # ^ The above line will not error out if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
            # are set, so it can't be used to check if we have a valid connection to S3. Instead:
            connection.get_all_buckets()
        except (boto.exception.S3ResponseError, boto.exception.NoAuthHandlerFound):
            s3_available = False
        else:
            s3_available = True
        finally:
            when_s3_available.s3_available = s3_available  # Cache result to avoid having to compute it again
    return unittest.skipIf(
        not s3_available, 'S3 is not available'
    )(function)


def when_exporter_available(function):
    return unittest.skipIf(
        os.getenv('EXPORTER') is None, 'Private Exporter code is not available'
    )(function)


def when_geolocation_data_available(function):
    config = get_test_config()
    geolocation_data = config.get('geolocation_data')
    geolocation_data_available = bool(geolocation_data)
    if geolocation_data_available:
        geolocation_data_available = get_target_from_url(get_jenkins_safe_url(geolocation_data)).exists()
    return unittest.skipIf(
        not geolocation_data_available, 'Geolocation data is not available'
    )(function)


def when_vertica_available(function):
    config = get_test_config()
    vertica_available = bool(config.get('vertica_creds_url'))
    return unittest.skipIf(
        not vertica_available, 'Vertica service is not available'
    )(function)


def when_vertica_not_available(function):
    config = get_test_config()
    vertica_available = bool(config.get('vertica_creds_url'))
    return unittest.skipIf(
        vertica_available, 'Vertica service is available'
    )(function)


# Utility functions


def get_test_config():
    config_json = os.getenv('ACCEPTANCE_TEST_CONFIG')
    try:
        with open(config_json, 'r') as config_json_file:
            config = json.load(config_json_file)
    except (IOError, TypeError):
        try:
            config = json.loads(config_json)
        except TypeError:
            config = {}
    return config


def get_jenkins_safe_url(url):
    # The machine running the acceptance test suite may not have hadoop installed on it, so convert S3 paths (which
    # are normally handled by the hadoop DFS client) to S3+https paths, which are handled by the python native S3
    # client.
    return url.replace('s3://', 's3+https://')


class AcceptanceTestCase(unittest.TestCase):

    acceptance = 1
    NUM_MAPPERS = 4
    NUM_REDUCERS = 2

    def setUp(self):
        try:
            self.s3_client = S3Client()
        except Exception:
            self.s3_client = None

        self.config = get_test_config()

        for env_var in ('TASKS_REPO', 'TASKS_BRANCH', 'IDENTIFIER', 'JOB_FLOW_NAME'):
            if env_var in os.environ:
                self.config[env_var.lower()] = os.environ[env_var]

        # The name of an existing job flow to run the test on
        assert('job_flow_name' in self.config or 'host' in self.config)
        # The git URL of the pipeline repository to check this code out from.
        assert('tasks_repo' in self.config)
        # The branch of the pipeline repository to test. Note this can differ from the branch that is currently
        # checked out and running this code.
        assert('tasks_branch' in self.config)
        # Where to store logs generated by the pipeline
        assert('tasks_log_path' in self.config)
        # The user to connect to the job flow over SSH with.
        assert('connection_user' in self.config)
        # Where the pipeline should output data, should be a URL pointing to a directory.
        assert('tasks_output_url' in self.config)
        # Allow for parallel execution of the test by specifying a different identifier. Using an identical identifier
        # allows for old virtualenvs to be reused etc, which is why a random one is not simply generated with each run.
        assert('identifier' in self.config)
        # A URL to a JSON file that contains most of the connection information for the MySQL database.
        assert('credentials_file_url' in self.config)
        # A URL to a build of the oddjob third party library
        assert 'oddjob_jar' in self.config
        # A URL to a maxmind compatible geolocation database file
        assert 'geolocation_data' in self.config

        self.data_dir = os.path.join(os.path.dirname(__file__), 'fixtures')

        url = self.config['tasks_output_url']
        m = hashlib.md5()
        m.update(self.config['identifier'])
        self.identifier = m.hexdigest()
        self.test_root = url_path_join(url, self.identifier, self.__class__.__name__)

        self.test_src = url_path_join(self.test_root, 'src')
        self.test_out = url_path_join(self.test_root, 'out')

        self.catalog_path = 'http://acceptance.test/api/courses/v2'
        database_name = 'test_' + self.identifier
        schema = 'test_' + self.identifier
        import_database_name = 'acceptance_import_' + database_name
        export_database_name = 'acceptance_export_' + database_name
        otto_database_name = 'acceptance_otto_' + database_name
        self.warehouse_path = url_path_join(self.test_root, 'warehouse')
        task_config_override = {
            'hive': {
                'database': database_name,
                'warehouse_path': self.warehouse_path
            },
            'map-reduce': {
                'marker': url_path_join(self.test_root, 'marker')
            },
            'manifest': {
                'path': url_path_join(self.test_root, 'manifest'),
                'lib_jar': self.config['oddjob_jar']
            },
            'database-import': {
                'credentials': self.config['credentials_file_url'],
                'destination': self.warehouse_path,
                'database': import_database_name
            },
            'database-export': {
                'credentials': self.config['credentials_file_url'],
                'database': export_database_name
            },
            'otto-database-import': {
                'credentials': self.config['credentials_file_url'],
                'database': otto_database_name
            },
            'course-catalog': {
                'catalog_path': self.catalog_path
            },
            'geolocation': {
                'geolocation_data': self.config['geolocation_data']
            },
            'event-logs': {
                'source': self.test_src
            },
            'course-structure': {
                'api_root_url': 'acceptance.test',
                'access_token': 'acceptance'
            }
        }
        if 'vertica_creds_url' in self.config:
            task_config_override['vertica-export'] = {
                'credentials': self.config['vertica_creds_url'],
                'schema': schema
            }
        if 'manifest_input_format' in self.config:
            task_config_override['manifest']['input_format'] = self.config['manifest_input_format']

        log.info('Running test: %s', self.id())
        log.info('Using executor: %s', self.config['identifier'])
        log.info('Generated Test Identifier: %s', self.identifier)

        self.import_db = db.DatabaseService(self.config, import_database_name)
        self.export_db = db.DatabaseService(self.config, export_database_name)
        self.otto_db = db.DatabaseService(self.config, otto_database_name)
        self.task = task.TaskService(self.config, task_config_override, self.identifier)
        self.hive = hive.HiveService(self.task, self.config, database_name)
        self.vertica = vertica.VerticaService(self.config, schema)

        if os.getenv('DISABLE_RESET_STATE', 'false').lower() != 'true':
            self.reset_external_state()

    def reset_external_state(self):
        root_target = get_target_from_url(get_jenkins_safe_url(self.test_root))
        if root_target.exists():
            root_target.remove()
        self.import_db.reset()
        self.export_db.reset()
        self.otto_db.reset()
        self.hive.reset()
        self.vertica.reset()

    def upload_tracking_log(self, input_file_name, file_date):
        # Define a tracking log path on S3 that will be matched by the standard event-log pattern."
        input_file_path = url_path_join(
            self.test_src,
            'FakeServerGroup',
            'tracking.log-{0}.gz'.format(file_date.strftime('%Y%m%d'))
        )
        with fs.gzipped_file(os.path.join(self.data_dir, 'input', input_file_name)) as compressed_file_name:
            self.upload_file(compressed_file_name, input_file_path)

    def upload_file(self, local_file_name, remote_file_path):
        log.debug('Uploading %s to %s', local_file_name, remote_file_path)
        with get_target_from_url(remote_file_path).open('w') as remote_file:
            with open(local_file_name, 'r') as local_file:
                shutil.copyfileobj(local_file, remote_file)

    def upload_file_with_content(self, remote_file_path, content):
        log.debug('Writing %s from string', remote_file_path)
        with get_target_from_url(remote_file_path).open('w') as remote_file:
            remote_file.write(content)

    def execute_sql_fixture_file(self, sql_file_name, database=None):
        if database is None:
            database = self.import_db
        log.debug('Executing SQL fixture %s on %s', sql_file_name, database.database_name)
        database.execute_sql_file(os.path.join(self.data_dir, 'input', sql_file_name))
