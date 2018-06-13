import itertools
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent

import pytest
import shutil
import structlog
import uuid
from datetime import datetime
from sqlalchemy import and_
from typing import Iterable
from typing import Tuple, NamedTuple, Optional, Mapping

import digitalearthau
import digitalearthau.system
from datacube.config import LocalConfig
from datacube.drivers.postgres import PostgresDb
from datacube.drivers.postgres import _api
from datacube.drivers.postgres import _core
from datacube.drivers.postgres import _dynamic
from datacube.index import Index
from datacube.model import Dataset
from digitalearthau import paths, collections
from digitalearthau.collections import Collection
from digitalearthau.index import DatasetLite, add_dataset
from digitalearthau.paths import register_base_directory
from digitalearthau.utils import CleanConsoleRenderer
from .utils import write_files

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

# These are unavoidable in pytests due to fixtures
# pylint: disable=redefined-outer-name,protected-access,invalid-name

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

# The default test config options.
# The user overrides these by creating their own file in ~/.datacube_integration.conf
INTEGRATION_DEFAULT_CONFIG_PATH = Path(__file__).parent.joinpath('deaintegration.conf')

INTEGRATION_TEST_DATA = Path(__file__).parent / 'data'

PROJECT_ROOT = Path(__file__).parents[1]

DEA_MD_TYPES = digitalearthau.CONFIG_DIR / 'metadata-types.yaml'
DEA_PRODUCTS_DIR = digitalearthau.CONFIG_DIR / 'products'


@pytest.fixture(scope="session", autouse=True)
def configure_log_output(request):
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M.%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Coloured output if to terminal.
            CleanConsoleRenderer()
        ],
        context_class=dict,
        cache_logger_on_first_use=True,
    )


@pytest.fixture(autouse=True)
def work_path(tmpdir):
    """Redirect the NCI Work Directory into a temporary directory for testing."""
    paths.NCI_WORK_ROOT = Path(tmpdir) / 'work'
    paths.NCI_WORK_ROOT.mkdir()
    # The default use of timestamp will collide when run quickly, as in unit tests.
    paths._JOB_WORK_OFFSET = '{output_product}-{task_type}-{request_uuid}'
    return paths.NCI_WORK_ROOT


@pytest.fixture
def integration_test_data(tmpdir):
    """A Path pointing to a copy of the `integration_data` directory."""
    temp_data_dir = Path(tmpdir) / 'integration_data'
    shutil.copytree(INTEGRATION_TEST_DATA, temp_data_dir)
    return temp_data_dir


ON_DISK2_ID = DatasetLite(uuid.UUID('10c4a9fe-2890-11e6-8ec8-a0000100fe80'))

ON_DISK2_OFFSET = ('LS8_OLITIRS_OTH_P51_GALPGS01-032_114_080_20150924', 'ga-metadata.yaml')


class DatasetForTests(NamedTuple):
    """
    A test dataset, including the file location and collection it should belong to.

    When your test starts the dataset will be on disk but not yet indexed. Call add_to_index() and others as needed.

    All properties are recorded here separately so tests can verify them independently.
    """
    # The test collection this should belong to
    collection: Collection

    id_: uuid.UUID

    # We separate path from a test base path for calculating trash prefixes etc.
    # You usually just want to use `self.path` instead.
    base_path: Path
    path_offset: Tuple[str, ...]

    # Source dataset that will be indexed if this is indexed (ie. embedded inside it)
    parent_id: uuid.UUID = None

    @property
    def path(self):
        return self.base_path.joinpath(*self.path_offset)

    @property
    def copyable_path(self):
        """Get the path containing the whole dataset that can be copied on disk.

        The recorded self.path of datasets is the path to the metadata, but "packaged" datasets
        such as scenes have a folder hierarchy, and to copy them we want to copy the whole scene
        folder, not just the metadata file.

        (This will return a folder for a scene, and will be identical to self.path for typical NetCDFs)
        """
        package_path, _ = paths.get_dataset_paths(self.path)
        return package_path

    @property
    def uri(self):
        return self.path.as_uri()

    @property
    def dataset(self):
        return DatasetLite(self.id_)

    @property
    def parent(self) -> Optional[DatasetLite]:
        """Source datasets that will be indexed if on_disk1 is indexed"""
        return DatasetLite(self.parent_id) if self.parent_id else None

    def add_to_index(self):
        """Add to the current collection's index"""
        add_dataset(self.collection.index_, self.id_, self.uri)

    def archive_in_index(self, archived_dt: datetime = None):
        archive_dataset(self.id_, self.collection, archived_dt=archived_dt)

    def archive_location_in_index(self, archived_dt: datetime = None, uri: str = None):
        archive_location(self.id_, uri or self.uri, self.collection, archived_dt=archived_dt)

    def add_location(self, uri: str) -> bool:
        return self.collection.index_.datasets.add_location(self.id_, uri)

    def get_index_record(self) -> Optional[Dataset]:
        """If this is indexed, return the full Dataset record"""
        return self.collection.index_.datasets.get(self.id_)


# We want one fixture to return all of this data. Returning a tuple was getting unwieldy.
class SimpleEnv(NamedTuple):
    collection: Collection

    on_disk1_id: uuid.UUID
    on_disk_uri: str

    base_test_path: Path


@pytest.fixture
def test_dataset(integration_test_data, dea_index) -> DatasetForTests:
    """A dataset on disk, with corresponding collection"""
    test_data = integration_test_data

    # Tests assume one dataset for the collection, so delete the second.
    shutil.rmtree(str(test_data.joinpath('LS8_OLITIRS_OTH_P51_GALPGS01-032_114_080_20150924')))
    ls8_collection = Collection(
        name='ls8_scene_test',
        query={},
        file_patterns=[str(test_data.joinpath('LS8*/ga-metadata.yaml'))],
        unique=[],
        index_=dea_index
    )
    collections._add(ls8_collection)

    # Add a decoy collection.
    ls5_nc_collection = Collection(
        name='ls5_nc_test',
        query={},
        file_patterns=[str(test_data.joinpath('LS5*.nc'))],
        unique=[],
        index_=dea_index
    )
    collections._add(ls5_nc_collection)

    # register this as a base directory so that datasets can be trashed within it.
    register_base_directory(str(test_data))

    cache_path = test_data.joinpath('cache')
    cache_path.mkdir()

    return DatasetForTests(
        collection=ls8_collection,
        id_=uuid.UUID('86150afc-b7d5-4938-a75e-3445007256d3'),
        base_path=test_data,
        path_offset=('LS8_OLITIRS_OTH_P51_GALPGS01-032_114_080_20160926', 'ga-metadata.yaml'),
        parent_id=uuid.UUID('dee471ed-5aa5-46f5-96b5-1e1ea91ffee4')
    )


@pytest.fixture
def other_dataset(integration_test_data: Path, test_dataset: DatasetForTests) -> DatasetForTests:
    """
    A dataset matching the same collection as test_dataset, but not indexed.
    """

    ds_id = uuid.UUID("5294efa6-348d-11e7-a079-185e0f80a5c0")
    write_files(
        {
            'LS8_INDEXED_ALREADY': {
                'ga-metadata.yaml':
                    dedent("""\
                        id: %s
                        platform:
                            code: LANDSAT_8
                        instrument:
                            name: OLI_TIRS
                        format:
                            name: GeoTIFF
                        product_type: level1
                        product_level: L1T
                        image:
                            bands: {}
                        lineage:
                            source_datasets: {}""" % str(ds_id)),
                'dummy-file.txt': ''
            }
        },
        containing_dir=integration_test_data
    )

    return DatasetForTests(
        collection=test_dataset.collection,
        id_=ds_id,
        base_path=integration_test_data,
        path_offset=('LS8_INDEXED_ALREADY', 'ga-metadata.yaml')
    )


def archive_dataset(dataset_id: uuid.UUID, collection: Collection, archived_dt: datetime = None):
    if archived_dt is None:
        collection.index_.datasets.archive([dataset_id])
    else:
        # Hack until ODC allows specifying the archive time.
        with collection.index_._db.begin() as transaction:
            # SQLAlchemy queries require "column == None", not "column is None" due to operator overloading:
            # pylint: disable=singleton-comparison
            transaction._connection.execute(
                _api.DATASET.update().where(
                    _api.DATASET.c.id == dataset_id
                ).where(
                    _api.DATASET.c.archived == None
                ).values(
                    archived=archived_dt
                )
            )


def archive_location(dataset_id: uuid.UUID, uri: str, collection: Collection, archived_dt: datetime = None):
    if archived_dt is None:
        collection.index_.datasets.archive_location(dataset_id, uri)
    else:
        scheme, body = _api._split_uri(uri)
        # Hack until ODC allows specifying the archive time.
        with collection.index_._db.begin() as transaction:
            # SQLAlchemy queries require "column == None", not "column is None" due to operator overloading:
            # pylint: disable=singleton-comparison
            transaction._connection.execute(
                _api.DATASET_LOCATION.update().where(
                    and_(
                        _api.DATASET_LOCATION.c.dataset_ref == dataset_id,
                        _api.DATASET_LOCATION.c.uri_scheme == scheme,
                        _api.DATASET_LOCATION.c.uri_body == body,
                        _api.DATASET_LOCATION.c.archived == None,
                    )
                ).values(
                    archived=archived_dt
                )
            )


def freeze_index(index: Index) -> Mapping[DatasetLite, Iterable[str]]:
    """
    All contained (dataset_id, [location]) values, to check test results.
    """
    return dict(
        (
            DatasetLite(dataset.id, archived_time=dataset.archived_time),
            tuple(dataset.uris)
        )
        for dataset in index.datasets.search()
    )


@pytest.fixture
def integration_config_paths():
    if not INTEGRATION_DEFAULT_CONFIG_PATH.exists():
        # Safety check. We never want it falling back to the default config,
        # as it will alter/wipe the user's own datacube to run tests
        raise RuntimeError('Integration default file not found. This should be built-in?')

    return (
        str(INTEGRATION_DEFAULT_CONFIG_PATH),
        os.path.expanduser('~/.datacube_integration.conf')
    )


@pytest.fixture
def global_integration_cli_args(integration_config_paths: Iterable[str]):
    """
    The first arguments to pass to a cli command for integration test configuration.
    """
    # List of a config files in order.
    return list(itertools.chain(*(('--config_file', f) for f in integration_config_paths)))


@pytest.fixture
def local_config(integration_config_paths):
    return LocalConfig.find(integration_config_paths)


def remove_dynamic_indexes():
    """
    Clear any dynamically created indexes from the schema.
    """
    # Our normal indexes start with "ix_", dynamic indexes with "dix_"
    for table in _core.METADATA.tables.values():
        table.indexes.intersection_update([i for i in table.indexes if not i.name.startswith('dix_')])


@contextmanager
def _increase_logging(log, level=logging.WARN):
    previous_level = log.getEffectiveLevel()
    log.setLevel(level)
    yield
    log.setLevel(previous_level)


@pytest.fixture
def db(local_config: LocalConfig):
    db = PostgresDb.from_config(local_config, application_name='dea-test-run', validate_connection=False)

    # Drop and recreate tables so our tests have a clean db.
    with db.connect() as connection:
        _core.drop_db(connection._connection)
    remove_dynamic_indexes()

    # Disable informational messages since we're doing this on every test run.
    with _increase_logging(_core._LOG) as _:
        _core.ensure_db(db._engine)

    # We don't need informational create/drop messages for every config change.
    _dynamic._LOG.setLevel(logging.WARN)

    yield db
    db.close()


@pytest.fixture
def index(db: PostgresDb):
    """
    :type db: datacube.drivers.postgres.PostgresDb
    """
    return Index(db)


@pytest.fixture
def dea_index(index: Index):
    """
    An index initialised with DEA config (products)
    """
    # Add DEA metadata types, products. They'll be validated too.
    digitalearthau.system.init_dea(
        index,
        with_permissions=False,
        # No "product added" logging as it makes test runs too noisy
        log_header=lambda *s: None,
        log=lambda *s: None,

    )

    return index