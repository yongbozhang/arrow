# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from collections import OrderedDict
import datetime
import decimal
import io
import json
import os
import six
import pickle
import pytest

import numpy as np

import pyarrow as pa
from pyarrow.compat import guid, u, BytesIO, unichar, PY2
from pyarrow.pandas_compat import _pandas_api
from pyarrow.tests import util
from pyarrow.filesystem import LocalFileSystem, FileSystem

try:
    import pyarrow.parquet as pq
except ImportError:
    pq = None


try:
    import pandas as pd
    import pandas.util.testing as tm
    from .pandas_examples import dataframe_with_arrays, dataframe_with_lists
except ImportError:
    pd = tm = None


# Marks all of the tests in this module
# Ignore these with pytest ... -m 'not parquet'
pytestmark = pytest.mark.parquet


@pytest.fixture(scope='module')
def datadir(datadir):
    return datadir / 'parquet'


def _write_table(table, path, **kwargs):
    # So we see the ImportError somewhere
    import pyarrow.parquet as pq

    if _pandas_api.is_data_frame(table):
        table = pa.Table.from_pandas(table)

    pq.write_table(table, path, **kwargs)
    return table


def _read_table(*args, **kwargs):
    return pq.read_table(*args, **kwargs)


def _roundtrip_table(table, read_table_kwargs=None,
                     write_table_kwargs=None):
    read_table_kwargs = read_table_kwargs or {}
    write_table_kwargs = write_table_kwargs or {}

    buf = io.BytesIO()
    _write_table(table, buf, **write_table_kwargs)
    buf.seek(0)
    return _read_table(buf, **read_table_kwargs)


def _check_roundtrip(table, expected=None, read_table_kwargs=None,
                     **write_table_kwargs):
    if expected is None:
        expected = table

    read_table_kwargs = read_table_kwargs or {}

    # intentionally check twice
    result = _roundtrip_table(table, read_table_kwargs=read_table_kwargs,
                              write_table_kwargs=write_table_kwargs)
    assert result.equals(expected)
    result = _roundtrip_table(result, read_table_kwargs=read_table_kwargs,
                              write_table_kwargs=write_table_kwargs)
    assert result.equals(expected)


def _roundtrip_pandas_dataframe(df, write_kwargs):
    table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(table, buf, **write_kwargs)

    buf.seek(0)
    table1 = _read_table(buf)
    return table1.to_pandas()


@pytest.mark.parametrize('dtype', [int, float])
def test_single_pylist_column_roundtrip(tempdir, dtype):
    filename = tempdir / 'single_{}_column.parquet'.format(dtype.__name__)
    data = [pa.array(list(map(dtype, range(5))))]
    table = pa.Table.from_arrays(data, names=['a'])
    _write_table(table, filename)
    table_read = _read_table(filename)
    for i in range(table.num_columns):
        col_written = table[i]
        col_read = table_read[i]
        assert table.field(i).name == table_read.field(i).name
        assert col_read.num_chunks == 1
        data_written = col_written.chunk(0)
        data_read = col_read.chunk(0)
        assert data_written.equals(data_read)


def alltypes_sample(size=10000, seed=0, categorical=False):
    np.random.seed(seed)
    arrays = {
        'uint8': np.arange(size, dtype=np.uint8),
        'uint16': np.arange(size, dtype=np.uint16),
        'uint32': np.arange(size, dtype=np.uint32),
        'uint64': np.arange(size, dtype=np.uint64),
        'int8': np.arange(size, dtype=np.int16),
        'int16': np.arange(size, dtype=np.int16),
        'int32': np.arange(size, dtype=np.int32),
        'int64': np.arange(size, dtype=np.int64),
        'float32': np.arange(size, dtype=np.float32),
        'float64': np.arange(size, dtype=np.float64),
        'bool': np.random.randn(size) > 0,
        # TODO(wesm): Test other timestamp resolutions now that arrow supports
        # them
        'datetime': np.arange("2016-01-01T00:00:00.001", size,
                              dtype='datetime64[ms]'),
        'str': pd.Series([str(x) for x in range(size)]),
        'empty_str': [''] * size,
        'str_with_nulls': [None] + [str(x) for x in range(size - 2)] + [None],
        'null': [None] * size,
        'null_list': [None] * 2 + [[None] * (x % 4) for x in range(size - 2)],
    }
    if categorical:
        arrays['str_category'] = arrays['str'].astype('category')
    return pd.DataFrame(arrays)


@pytest.mark.pandas
@pytest.mark.parametrize('chunk_size', [None, 1000])
def test_pandas_parquet_2_0_roundtrip(tempdir, chunk_size):
    df = alltypes_sample(size=10000, categorical=True)

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df)
    assert arrow_table.schema.pandas_metadata is not None

    _write_table(arrow_table, filename, version="2.0",
                 coerce_timestamps='ms', chunk_size=chunk_size)
    table_read = pq.read_pandas(filename)
    assert table_read.schema.pandas_metadata is not None

    assert arrow_table.schema.metadata == table_read.schema.metadata

    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


def test_set_data_page_size():
    arr = pa.array([1, 2, 3] * 1000000)
    t = pa.Table.from_arrays([arr], names=['f0'])

    # 128K, 256K, 512K
    page_sizes = [2 << 16, 2 << 17, 2 << 18]
    for target_page_size in page_sizes:
        _check_roundtrip(t, data_page_size=target_page_size)


@pytest.mark.pandas
def test_chunked_table_write():
    # ARROW-232
    df = alltypes_sample(size=10)

    batch = pa.RecordBatch.from_pandas(df)
    table = pa.Table.from_batches([batch] * 3)
    _check_roundtrip(table, version='2.0')

    df, _ = dataframe_with_lists()
    batch = pa.RecordBatch.from_pandas(df)
    table = pa.Table.from_batches([batch] * 3)
    _check_roundtrip(table, version='2.0')


@pytest.mark.pandas
def test_no_memory_map(tempdir):
    df = alltypes_sample(size=10)

    table = pa.Table.from_pandas(df)
    _check_roundtrip(table, read_table_kwargs={'memory_map': False},
                     version='2.0')

    filename = str(tempdir / 'tmp_file')
    with open(filename, 'wb') as f:
        _write_table(table, f, version='2.0')
    table_read = pq.read_pandas(filename, memory_map=False)
    assert table_read.equals(table)


def test_special_chars_filename(tempdir):
    table = pa.Table.from_arrays([pa.array([42])], ["ints"])
    filename = "foo # bar"
    path = tempdir / filename
    assert not path.exists()
    _write_table(table, str(path))
    assert path.exists()
    table_read = _read_table(str(path))
    assert table_read.equals(table)


@pytest.mark.pandas
def test_empty_table_roundtrip():
    df = alltypes_sample(size=10)

    # Create a non-empty table to infer the types correctly, then slice to 0
    table = pa.Table.from_pandas(df)
    table = pa.Table.from_arrays(
        [col.chunk(0)[:0] for col in table.itercolumns()],
        names=table.schema.names)

    assert table.schema.field('null').type == pa.null()
    assert table.schema.field('null_list').type == pa.list_(pa.null())
    _check_roundtrip(table, version='2.0')


@pytest.mark.pandas
def test_empty_table_no_columns():
    df = pd.DataFrame()
    empty = pa.Table.from_pandas(df, preserve_index=False)
    _check_roundtrip(empty)


def test_empty_lists_table_roundtrip():
    # ARROW-2744: Shouldn't crash when writing an array of empty lists
    arr = pa.array([[], []], type=pa.list_(pa.int32()))
    table = pa.Table.from_arrays([arr], ["A"])
    _check_roundtrip(table)


@pytest.mark.pandas
def test_pandas_parquet_datetime_tz():
    s = pd.Series([datetime.datetime(2017, 9, 6)])
    s = s.dt.tz_localize('utc')

    s.index = s

    # Both a column and an index to hit both use cases
    df = pd.DataFrame({'tz_aware': s,
                       'tz_eastern': s.dt.tz_convert('US/Eastern')},
                      index=s)

    f = BytesIO()

    arrow_table = pa.Table.from_pandas(df)

    _write_table(arrow_table, f, coerce_timestamps='ms')
    f.seek(0)

    table_read = pq.read_pandas(f)

    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
@pytest.mark.skipif(six.PY2, reason='datetime.timezone is available since '
                                    'python version 3.2')
def test_datetime_timezone_tzinfo():
    value = datetime.datetime(2018, 1, 1, 1, 23, 45,
                              tzinfo=datetime.timezone.utc)
    df = pd.DataFrame({'foo': [value]})

    _roundtrip_pandas_dataframe(df, write_kwargs={})


@pytest.mark.pandas
def test_pandas_parquet_custom_metadata(tempdir):
    df = alltypes_sample(size=10000)

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df)
    assert b'pandas' in arrow_table.schema.metadata

    _write_table(arrow_table, filename, version='2.0', coerce_timestamps='ms')

    metadata = pq.read_metadata(filename).metadata
    assert b'pandas' in metadata

    js = json.loads(metadata[b'pandas'].decode('utf8'))
    assert js['index_columns'] == [{'kind': 'range',
                                    'name': None,
                                    'start': 0, 'stop': 10000,
                                    'step': 1}]


@pytest.mark.pandas
def test_pandas_parquet_column_multiindex(tempdir):
    df = alltypes_sample(size=10)
    df.columns = pd.MultiIndex.from_tuples(
        list(zip(df.columns, df.columns[::-1])),
        names=['level_1', 'level_2']
    )

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df)
    assert arrow_table.schema.pandas_metadata is not None

    _write_table(arrow_table, filename, version='2.0', coerce_timestamps='ms')

    table_read = pq.read_pandas(filename)
    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_pandas_parquet_2_0_roundtrip_read_pandas_no_index_written(tempdir):
    df = alltypes_sample(size=10000)

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df, preserve_index=False)
    js = arrow_table.schema.pandas_metadata
    assert not js['index_columns']
    # ARROW-2170
    # While index_columns should be empty, columns needs to be filled still.
    assert js['columns']

    _write_table(arrow_table, filename, version='2.0', coerce_timestamps='ms')
    table_read = pq.read_pandas(filename)

    js = table_read.schema.pandas_metadata
    assert not js['index_columns']

    assert arrow_table.schema.metadata == table_read.schema.metadata

    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_pandas_parquet_1_0_roundtrip(tempdir):
    size = 10000
    np.random.seed(0)
    df = pd.DataFrame({
        'uint8': np.arange(size, dtype=np.uint8),
        'uint16': np.arange(size, dtype=np.uint16),
        'uint32': np.arange(size, dtype=np.uint32),
        'uint64': np.arange(size, dtype=np.uint64),
        'int8': np.arange(size, dtype=np.int16),
        'int16': np.arange(size, dtype=np.int16),
        'int32': np.arange(size, dtype=np.int32),
        'int64': np.arange(size, dtype=np.int64),
        'float32': np.arange(size, dtype=np.float32),
        'float64': np.arange(size, dtype=np.float64),
        'bool': np.random.randn(size) > 0,
        'str': [str(x) for x in range(size)],
        'str_with_nulls': [None] + [str(x) for x in range(size - 2)] + [None],
        'empty_str': [''] * size
    })
    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df)
    _write_table(arrow_table, filename, version='1.0')
    table_read = _read_table(filename)
    df_read = table_read.to_pandas()

    # We pass uint32_t as int64_t if we write Parquet version 1.0
    df['uint32'] = df['uint32'].values.astype(np.int64)

    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_multiple_path_types(tempdir):
    # Test compatibility with PEP 519 path-like objects
    path = tempdir / 'zzz.parquet'
    df = pd.DataFrame({'x': np.arange(10, dtype=np.int64)})
    _write_table(df, path)
    table_read = _read_table(path)
    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)

    # Test compatibility with plain string paths
    path = str(tempdir) + 'zzz.parquet'
    df = pd.DataFrame({'x': np.arange(10, dtype=np.int64)})
    _write_table(df, path)
    table_read = _read_table(path)
    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_pandas_column_selection(tempdir):
    size = 10000
    np.random.seed(0)
    df = pd.DataFrame({
        'uint8': np.arange(size, dtype=np.uint8),
        'uint16': np.arange(size, dtype=np.uint16)
    })
    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df)
    _write_table(arrow_table, filename)
    table_read = _read_table(filename, columns=['uint8'])
    df_read = table_read.to_pandas()

    tm.assert_frame_equal(df[['uint8']], df_read)

    # ARROW-4267: Selection of duplicate columns still leads to these columns
    # being read uniquely.
    table_read = _read_table(filename, columns=['uint8', 'uint8'])
    df_read = table_read.to_pandas()

    tm.assert_frame_equal(df[['uint8']], df_read)


def _random_integers(size, dtype):
    # We do not generate integers outside the int64 range
    platform_int_info = np.iinfo('int_')
    iinfo = np.iinfo(dtype)
    return np.random.randint(max(iinfo.min, platform_int_info.min),
                             min(iinfo.max, platform_int_info.max),
                             size=size).astype(dtype)


def _test_dataframe(size=10000, seed=0):
    np.random.seed(seed)
    df = pd.DataFrame({
        'uint8': _random_integers(size, np.uint8),
        'uint16': _random_integers(size, np.uint16),
        'uint32': _random_integers(size, np.uint32),
        'uint64': _random_integers(size, np.uint64),
        'int8': _random_integers(size, np.int8),
        'int16': _random_integers(size, np.int16),
        'int32': _random_integers(size, np.int32),
        'int64': _random_integers(size, np.int64),
        'float32': np.random.randn(size).astype(np.float32),
        'float64': np.arange(size, dtype=np.float64),
        'bool': np.random.randn(size) > 0,
        'strings': [tm.rands(10) for i in range(size)],
        'all_none': [None] * size,
        'all_none_category': [None] * size
    })
    # TODO(PARQUET-1015)
    # df['all_none_category'] = df['all_none_category'].astype('category')
    return df


@pytest.mark.pandas
def test_pandas_parquet_native_file_roundtrip(tempdir):
    df = _test_dataframe(10000)
    arrow_table = pa.Table.from_pandas(df)
    imos = pa.BufferOutputStream()
    _write_table(arrow_table, imos, version="2.0")
    buf = imos.getvalue()
    reader = pa.BufferReader(buf)
    df_read = _read_table(reader).to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_parquet_incremental_file_build(tempdir):
    df = _test_dataframe(100)
    df['unique_id'] = 0

    arrow_table = pa.Table.from_pandas(df, preserve_index=False)
    out = pa.BufferOutputStream()

    writer = pq.ParquetWriter(out, arrow_table.schema, version='2.0')

    frames = []
    for i in range(10):
        df['unique_id'] = i
        arrow_table = pa.Table.from_pandas(df, preserve_index=False)
        writer.write_table(arrow_table)

        frames.append(df.copy())

    writer.close()

    buf = out.getvalue()
    result = _read_table(pa.BufferReader(buf))

    expected = pd.concat(frames, ignore_index=True)
    tm.assert_frame_equal(result.to_pandas(), expected)


@pytest.mark.pandas
def test_read_pandas_column_subset(tempdir):
    df = _test_dataframe(10000)
    arrow_table = pa.Table.from_pandas(df)
    imos = pa.BufferOutputStream()
    _write_table(arrow_table, imos, version="2.0")
    buf = imos.getvalue()
    reader = pa.BufferReader(buf)
    df_read = pq.read_pandas(reader, columns=['strings', 'uint8']).to_pandas()
    tm.assert_frame_equal(df[['strings', 'uint8']], df_read)


@pytest.mark.pandas
def test_pandas_parquet_empty_roundtrip(tempdir):
    df = _test_dataframe(0)
    arrow_table = pa.Table.from_pandas(df)
    imos = pa.BufferOutputStream()
    _write_table(arrow_table, imos, version="2.0")
    buf = imos.getvalue()
    reader = pa.BufferReader(buf)
    df_read = _read_table(reader).to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_pandas_parquet_pyfile_roundtrip(tempdir):
    filename = tempdir / 'pandas_pyfile_roundtrip.parquet'
    size = 5
    df = pd.DataFrame({
        'int64': np.arange(size, dtype=np.int64),
        'float32': np.arange(size, dtype=np.float32),
        'float64': np.arange(size, dtype=np.float64),
        'bool': np.random.randn(size) > 0,
        'strings': ['foo', 'bar', None, 'baz', 'qux']
    })

    arrow_table = pa.Table.from_pandas(df)

    with filename.open('wb') as f:
        _write_table(arrow_table, f, version="1.0")

    data = io.BytesIO(filename.read_bytes())

    table_read = _read_table(data)
    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_pandas_parquet_configuration_options(tempdir):
    size = 10000
    np.random.seed(0)
    df = pd.DataFrame({
        'uint8': np.arange(size, dtype=np.uint8),
        'uint16': np.arange(size, dtype=np.uint16),
        'uint32': np.arange(size, dtype=np.uint32),
        'uint64': np.arange(size, dtype=np.uint64),
        'int8': np.arange(size, dtype=np.int16),
        'int16': np.arange(size, dtype=np.int16),
        'int32': np.arange(size, dtype=np.int32),
        'int64': np.arange(size, dtype=np.int64),
        'float32': np.arange(size, dtype=np.float32),
        'float64': np.arange(size, dtype=np.float64),
        'bool': np.random.randn(size) > 0
    })
    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df)

    for use_dictionary in [True, False]:
        _write_table(arrow_table, filename, version='2.0',
                     use_dictionary=use_dictionary)
        table_read = _read_table(filename)
        df_read = table_read.to_pandas()
        tm.assert_frame_equal(df, df_read)

    for write_statistics in [True, False]:
        _write_table(arrow_table, filename, version='2.0',
                     write_statistics=write_statistics)
        table_read = _read_table(filename)
        df_read = table_read.to_pandas()
        tm.assert_frame_equal(df, df_read)

    for compression in ['NONE', 'SNAPPY', 'GZIP', 'LZ4', 'ZSTD']:
        _write_table(arrow_table, filename, version='2.0',
                     compression=compression)
        table_read = _read_table(filename)
        df_read = table_read.to_pandas()
        tm.assert_frame_equal(df, df_read)


def make_sample_file(table_or_df):
    if isinstance(table_or_df, pa.Table):
        a_table = table_or_df
    else:
        a_table = pa.Table.from_pandas(table_or_df)

    buf = io.BytesIO()
    _write_table(a_table, buf, compression='SNAPPY', version='2.0',
                 coerce_timestamps='ms')

    buf.seek(0)
    return pq.ParquetFile(buf)


@pytest.mark.pandas
def test_parquet_metadata_api():
    df = alltypes_sample(size=10000)
    df = df.reindex(columns=sorted(df.columns))
    df.index = np.random.randint(0, 1000000, size=len(df))

    fileh = make_sample_file(df)
    ncols = len(df.columns)

    # Series of sniff tests
    meta = fileh.metadata
    repr(meta)
    assert meta.num_rows == len(df)
    assert meta.num_columns == ncols + 1  # +1 for index
    assert meta.num_row_groups == 1
    assert meta.format_version == '2.0'
    assert 'parquet-cpp' in meta.created_by
    assert isinstance(meta.serialized_size, int)
    assert isinstance(meta.metadata, dict)

    # Schema
    schema = fileh.schema
    assert meta.schema is schema
    assert len(schema) == ncols + 1  # +1 for index
    repr(schema)

    col = schema[0]
    repr(col)
    assert col.name == df.columns[0]
    assert col.max_definition_level == 1
    assert col.max_repetition_level == 0
    assert col.max_repetition_level == 0

    assert col.physical_type == 'BOOLEAN'
    assert col.converted_type == 'NONE'

    with pytest.raises(IndexError):
        schema[ncols + 1]  # +1 for index

    with pytest.raises(IndexError):
        schema[-1]

    # Row group
    for rg in range(meta.num_row_groups):
        rg_meta = meta.row_group(rg)
        assert isinstance(rg_meta, pq.RowGroupMetaData)
        repr(rg_meta)

        for col in range(rg_meta.num_columns):
            col_meta = rg_meta.column(col)
            assert isinstance(col_meta, pq.ColumnChunkMetaData)
            repr(col_meta)

    with pytest.raises(IndexError):
        meta.row_group(-1)

    with pytest.raises(IndexError):
        meta.row_group(meta.num_row_groups + 1)

    rg_meta = meta.row_group(0)
    assert rg_meta.num_rows == len(df)
    assert rg_meta.num_columns == ncols + 1  # +1 for index
    assert rg_meta.total_byte_size > 0

    with pytest.raises(IndexError):
        col_meta = rg_meta.column(-1)

    with pytest.raises(IndexError):
        col_meta = rg_meta.column(ncols + 2)

    col_meta = rg_meta.column(0)
    assert col_meta.file_offset > 0
    assert col_meta.file_path == ''  # created from BytesIO
    assert col_meta.physical_type == 'BOOLEAN'
    assert col_meta.num_values == 10000
    assert col_meta.path_in_schema == 'bool'
    assert col_meta.is_stats_set is True
    assert isinstance(col_meta.statistics, pq.Statistics)
    assert col_meta.compression == 'SNAPPY'
    assert col_meta.encodings == ('PLAIN', 'RLE')
    assert col_meta.has_dictionary_page is False
    assert col_meta.dictionary_page_offset is None
    assert col_meta.data_page_offset > 0
    assert col_meta.total_compressed_size > 0
    assert col_meta.total_uncompressed_size > 0
    with pytest.raises(NotImplementedError):
        col_meta.has_index_page
    with pytest.raises(NotImplementedError):
        col_meta.index_page_offset


@pytest.mark.pandas
@pytest.mark.parametrize(
    (
        'data',
        'type',
        'physical_type',
        'min_value',
        'max_value',
        'null_count',
        'num_values',
        'distinct_count'
    ),
    [
        ([1, 2, 2, None, 4], pa.uint8(), 'INT32', 1, 4, 1, 4, 0),
        ([1, 2, 2, None, 4], pa.uint16(), 'INT32', 1, 4, 1, 4, 0),
        ([1, 2, 2, None, 4], pa.uint32(), 'INT32', 1, 4, 1, 4, 0),
        ([1, 2, 2, None, 4], pa.uint64(), 'INT64', 1, 4, 1, 4, 0),
        ([-1, 2, 2, None, 4], pa.int8(), 'INT32', -1, 4, 1, 4, 0),
        ([-1, 2, 2, None, 4], pa.int16(), 'INT32', -1, 4, 1, 4, 0),
        ([-1, 2, 2, None, 4], pa.int32(), 'INT32', -1, 4, 1, 4, 0),
        ([-1, 2, 2, None, 4], pa.int64(), 'INT64', -1, 4, 1, 4, 0),
        (
            [-1.1, 2.2, 2.3, None, 4.4], pa.float32(),
            'FLOAT', -1.1, 4.4, 1, 4, 0
        ),
        (
            [-1.1, 2.2, 2.3, None, 4.4], pa.float64(),
            'DOUBLE', -1.1, 4.4, 1, 4, 0
        ),
        (
            [u'', u'b', unichar(1000), None, u'aaa'], pa.binary(),
            'BYTE_ARRAY', b'', unichar(1000).encode('utf-8'), 1, 4, 0
        ),
        (
            [True, False, False, True, True], pa.bool_(),
            'BOOLEAN', False, True, 0, 5, 0
        ),
        (
            [b'\x00', b'b', b'12', None, b'aaa'], pa.binary(),
            'BYTE_ARRAY', b'\x00', b'b', 1, 4, 0
        ),
    ]
)
def test_parquet_column_statistics_api(data, type, physical_type, min_value,
                                       max_value, null_count, num_values,
                                       distinct_count):
    df = pd.DataFrame({'data': data})
    schema = pa.schema([pa.field('data', type)])
    table = pa.Table.from_pandas(df, schema=schema, safe=False)
    fileh = make_sample_file(table)

    meta = fileh.metadata

    rg_meta = meta.row_group(0)
    col_meta = rg_meta.column(0)

    stat = col_meta.statistics
    assert stat.has_min_max
    assert _close(type, stat.min, min_value)
    assert _close(type, stat.max, max_value)
    assert stat.null_count == null_count
    assert stat.num_values == num_values
    # TODO(kszucs) until parquet-cpp API doesn't expose HasDistinctCount
    # method, missing distinct_count is represented as zero instead of None
    assert stat.distinct_count == distinct_count
    assert stat.physical_type == physical_type


def _close(type, left, right):
    if type == pa.float32():
        return abs(left - right) < 1E-7
    elif type == pa.float64():
        return abs(left - right) < 1E-13
    else:
        return left == right


def test_statistics_convert_logical_types(tempdir):
    # ARROW-5166, ARROW-4139

    # (min, max, type)
    cases = [(10, 11164359321221007157, pa.uint64()),
             (10, 4294967295, pa.uint32()),
             (u"ähnlich", u"öffentlich", pa.utf8()),
             (datetime.time(10, 30, 0, 1000), datetime.time(15, 30, 0, 1000),
              pa.time32('ms')),
             (datetime.time(10, 30, 0, 1000), datetime.time(15, 30, 0, 1000),
              pa.time64('us')),
             (datetime.datetime(2019, 6, 24, 0, 0, 0, 1000),
              datetime.datetime(2019, 6, 25, 0, 0, 0, 1000),
              pa.timestamp('ms')),
             (datetime.datetime(2019, 6, 24, 0, 0, 0, 1000),
              datetime.datetime(2019, 6, 25, 0, 0, 0, 1000),
              pa.timestamp('us'))]

    for i, (min_val, max_val, typ) in enumerate(cases):
        t = pa.Table.from_arrays([pa.array([min_val, max_val], type=typ)],
                                 ['col'])
        path = str(tempdir / ('example{}.parquet'.format(i)))
        pq.write_table(t, path, version='2.0')
        pf = pq.ParquetFile(path)
        stats = pf.metadata.row_group(0).column(0).statistics
        assert stats.min == min_val
        assert stats.max == max_val


def test_parquet_write_disable_statistics(tempdir):
    table = pa.Table.from_pydict(
        {'a': pa.array([1, 2, 3]), 'b': pa.array(['a', 'b', 'c'])})
    _write_table(table, tempdir / 'data.parquet')
    meta = pq.read_metadata(tempdir / 'data.parquet')
    for col in [0, 1]:
        cc = meta.row_group(0).column(col)
        assert cc.is_stats_set is True
        assert cc.statistics is not None

    _write_table(table, tempdir / 'data2.parquet', write_statistics=False)
    meta = pq.read_metadata(tempdir / 'data2.parquet')
    for col in [0, 1]:
        cc = meta.row_group(0).column(col)
        assert cc.is_stats_set is False
        assert cc.statistics is None

    _write_table(table, tempdir / 'data3.parquet', write_statistics=['a'])
    meta = pq.read_metadata(tempdir / 'data3.parquet')
    cc_a = meta.row_group(0).column(0)
    assert cc_a.is_stats_set is True
    assert cc_a.statistics is not None
    cc_b = meta.row_group(0).column(1)
    assert cc_b.is_stats_set is False
    assert cc_b.statistics is None


@pytest.mark.pandas
def test_compare_schemas():
    df = alltypes_sample(size=10000)

    fileh = make_sample_file(df)
    fileh2 = make_sample_file(df)
    fileh3 = make_sample_file(df[df.columns[::2]])

    # ParquetSchema
    assert isinstance(fileh.schema, pq.ParquetSchema)
    assert fileh.schema.equals(fileh.schema)
    assert fileh.schema == fileh.schema
    assert fileh.schema.equals(fileh2.schema)
    assert fileh.schema == fileh2.schema
    assert fileh.schema != 'arbitrary object'
    assert not fileh.schema.equals(fileh3.schema)
    assert fileh.schema != fileh3.schema

    # ColumnSchema
    assert isinstance(fileh.schema[0], pq.ColumnSchema)
    assert fileh.schema[0].equals(fileh.schema[0])
    assert fileh.schema[0] == fileh.schema[0]
    assert not fileh.schema[0].equals(fileh.schema[1])
    assert fileh.schema[0] != fileh.schema[1]
    assert fileh.schema[0] != 'arbitrary object'


def test_validate_schema_write_table(tempdir):
    # ARROW-2926
    simple_fields = [
        pa.field('POS', pa.uint32()),
        pa.field('desc', pa.string())
    ]

    simple_schema = pa.schema(simple_fields)

    # simple_table schema does not match simple_schema
    simple_from_array = [pa.array([1]), pa.array(['bla'])]
    simple_table = pa.Table.from_arrays(simple_from_array, ['POS', 'desc'])

    path = tempdir / 'simple_validate_schema.parquet'

    with pq.ParquetWriter(path, simple_schema,
                          version='2.0',
                          compression='snappy', flavor='spark') as w:
        with pytest.raises(ValueError):
            w.write_table(simple_table)


@pytest.mark.pandas
def test_column_of_arrays(tempdir):
    df, schema = dataframe_with_arrays()

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df, schema=schema)
    _write_table(arrow_table, filename, version="2.0", coerce_timestamps='ms')
    table_read = _read_table(filename)
    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_coerce_timestamps(tempdir):
    from collections import OrderedDict
    # ARROW-622
    arrays = OrderedDict()
    fields = [pa.field('datetime64',
                       pa.list_(pa.timestamp('ms')))]
    arrays['datetime64'] = [
        np.array(['2007-07-13T01:23:34.123456789',
                  None,
                  '2010-08-13T05:46:57.437699912'],
                 dtype='datetime64[ms]'),
        None,
        None,
        np.array(['2007-07-13T02',
                  None,
                  '2010-08-13T05:46:57.437699912'],
                 dtype='datetime64[ms]'),
    ]

    df = pd.DataFrame(arrays)
    schema = pa.schema(fields)

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df, schema=schema)

    _write_table(arrow_table, filename, version="2.0", coerce_timestamps='us')
    table_read = _read_table(filename)
    df_read = table_read.to_pandas()

    df_expected = df.copy()
    for i, x in enumerate(df_expected['datetime64']):
        if isinstance(x, np.ndarray):
            df_expected['datetime64'][i] = x.astype('M8[us]')

    tm.assert_frame_equal(df_expected, df_read)

    with pytest.raises(ValueError):
        _write_table(arrow_table, filename, version='2.0',
                     coerce_timestamps='unknown')


@pytest.mark.pandas
def test_coerce_timestamps_truncated(tempdir):
    """
    ARROW-2555: Test that we can truncate timestamps when coercing if
    explicitly allowed.
    """
    dt_us = datetime.datetime(year=2017, month=1, day=1, hour=1, minute=1,
                              second=1, microsecond=1)
    dt_ms = datetime.datetime(year=2017, month=1, day=1, hour=1, minute=1,
                              second=1)

    fields_us = [pa.field('datetime64', pa.timestamp('us'))]
    arrays_us = {'datetime64': [dt_us, dt_ms]}

    df_us = pd.DataFrame(arrays_us)
    schema_us = pa.schema(fields_us)

    filename = tempdir / 'pandas_truncated.parquet'
    table_us = pa.Table.from_pandas(df_us, schema=schema_us)

    _write_table(table_us, filename, version="2.0", coerce_timestamps='ms',
                 allow_truncated_timestamps=True)
    table_ms = _read_table(filename)
    df_ms = table_ms.to_pandas()

    arrays_expected = {'datetime64': [dt_ms, dt_ms]}
    df_expected = pd.DataFrame(arrays_expected)
    tm.assert_frame_equal(df_expected, df_ms)


@pytest.mark.pandas
def test_column_of_lists(tempdir):
    df, schema = dataframe_with_lists(parquet_compatible=True)

    filename = tempdir / 'pandas_roundtrip.parquet'
    arrow_table = pa.Table.from_pandas(df, schema=schema)
    _write_table(arrow_table, filename, version='2.0')
    table_read = _read_table(filename)
    df_read = table_read.to_pandas()

    if PY2:
        # assert_frame_equal fails when comparing datetime.date and
        # np.datetime64, even with check_datetimelike_compat=True so
        # convert the values to np.datetime64 instead
        for col in ['date32[day]_list', 'date64[ms]_list']:
            df[col] = df[col].apply(
                lambda x: list(map(np.datetime64, x)) if x else x
            )

    tm.assert_frame_equal(df, df_read)


@pytest.mark.pandas
def test_date_time_types(tempdir):
    t1 = pa.date32()
    data1 = np.array([17259, 17260, 17261], dtype='int32')
    a1 = pa.array(data1, type=t1)

    t2 = pa.date64()
    data2 = data1.astype('int64') * 86400000
    a2 = pa.array(data2, type=t2)

    t3 = pa.timestamp('us')
    start = pd.Timestamp('2001-01-01').value / 1000
    data3 = np.array([start, start + 1, start + 2], dtype='int64')
    a3 = pa.array(data3, type=t3)

    t4 = pa.time32('ms')
    data4 = np.arange(3, dtype='i4')
    a4 = pa.array(data4, type=t4)

    t5 = pa.time64('us')
    a5 = pa.array(data4.astype('int64'), type=t5)

    t6 = pa.time32('s')
    a6 = pa.array(data4, type=t6)

    ex_t6 = pa.time32('ms')
    ex_a6 = pa.array(data4 * 1000, type=ex_t6)

    t7 = pa.timestamp('ns')
    start = pd.Timestamp('2001-01-01').value
    data7 = np.array([start, start + 1000, start + 2000],
                     dtype='int64')
    a7 = pa.array(data7, type=t7)

    table = pa.Table.from_arrays([a1, a2, a3, a4, a5, a6, a7],
                                 ['date32', 'date64', 'timestamp[us]',
                                  'time32[s]', 'time64[us]',
                                  'time32_from64[s]',
                                  'timestamp[ns]'])

    # date64 as date32
    # time32[s] to time32[ms]
    expected = pa.Table.from_arrays([a1, a1, a3, a4, a5, ex_a6, a7],
                                    ['date32', 'date64', 'timestamp[us]',
                                     'time32[s]', 'time64[us]',
                                     'time32_from64[s]',
                                     'timestamp[ns]'])

    _check_roundtrip(table, expected=expected, version='2.0')

    t0 = pa.timestamp('ms')
    data0 = np.arange(4, dtype='int64')
    a0 = pa.array(data0, type=t0)

    t1 = pa.timestamp('us')
    data1 = np.arange(4, dtype='int64')
    a1 = pa.array(data1, type=t1)

    t2 = pa.timestamp('ns')
    data2 = np.arange(4, dtype='int64')
    a2 = pa.array(data2, type=t2)

    table = pa.Table.from_arrays([a0, a1, a2],
                                 ['ts[ms]', 'ts[us]', 'ts[ns]'])
    expected = pa.Table.from_arrays([a0, a1, a2],
                                    ['ts[ms]', 'ts[us]', 'ts[ns]'])

    # int64 for all timestamps supported by default
    filename = tempdir / 'int64_timestamps.parquet'
    _write_table(table, filename, version='2.0')
    parquet_schema = pq.ParquetFile(filename).schema
    for i in range(3):
        assert parquet_schema.column(i).physical_type == 'INT64'
    read_table = _read_table(filename)
    assert read_table.equals(expected)

    t0_ns = pa.timestamp('ns')
    data0_ns = np.array(data0 * 1000000, dtype='int64')
    a0_ns = pa.array(data0_ns, type=t0_ns)

    t1_ns = pa.timestamp('ns')
    data1_ns = np.array(data1 * 1000, dtype='int64')
    a1_ns = pa.array(data1_ns, type=t1_ns)

    expected = pa.Table.from_arrays([a0_ns, a1_ns, a2],
                                    ['ts[ms]', 'ts[us]', 'ts[ns]'])

    # int96 nanosecond timestamps produced upon request
    filename = tempdir / 'explicit_int96_timestamps.parquet'
    _write_table(table, filename, version='2.0',
                 use_deprecated_int96_timestamps=True)
    parquet_schema = pq.ParquetFile(filename).schema
    for i in range(3):
        assert parquet_schema.column(i).physical_type == 'INT96'
    read_table = _read_table(filename)
    assert read_table.equals(expected)

    # int96 nanosecond timestamps implied by flavor 'spark'
    filename = tempdir / 'spark_int96_timestamps.parquet'
    _write_table(table, filename, version='2.0',
                 flavor='spark')
    parquet_schema = pq.ParquetFile(filename).schema
    for i in range(3):
        assert parquet_schema.column(i).physical_type == 'INT96'
    read_table = _read_table(filename)
    assert read_table.equals(expected)


def test_timestamp_restore_timezone():
    # ARROW-5888, restore timezone from serialized metadata
    ty = pa.timestamp('ms', tz='America/New_York')
    arr = pa.array([1, 2, 3], type=ty)
    t = pa.table([arr], names=['f0'])
    _check_roundtrip(t)


@pytest.mark.pandas
def test_list_of_datetime_time_roundtrip():
    # ARROW-4135
    times = pd.to_datetime(['09:00', '09:30', '10:00', '10:30', '11:00',
                            '11:30', '12:00'])
    df = pd.DataFrame({'time': [times.time]})
    _roundtrip_pandas_dataframe(df, write_kwargs={})


@pytest.mark.pandas
def test_parquet_version_timestamp_differences():
    i_s = pd.Timestamp('2010-01-01').value / 1000000000  # := 1262304000

    d_s = np.arange(i_s, i_s + 10, 1, dtype='int64')
    d_ms = d_s * 1000
    d_us = d_ms * 1000
    d_ns = d_us * 1000

    a_s = pa.array(d_s, type=pa.timestamp('s'))
    a_ms = pa.array(d_ms, type=pa.timestamp('ms'))
    a_us = pa.array(d_us, type=pa.timestamp('us'))
    a_ns = pa.array(d_ns, type=pa.timestamp('ns'))

    names = ['ts:s', 'ts:ms', 'ts:us', 'ts:ns']
    table = pa.Table.from_arrays([a_s, a_ms, a_us, a_ns], names)

    # Using Parquet version 1.0, seconds should be coerced to milliseconds
    # and nanoseconds should be coerced to microseconds by default
    expected = pa.Table.from_arrays([a_ms, a_ms, a_us, a_us], names)
    _check_roundtrip(table, expected)

    # Using Parquet version 2.0, seconds should be coerced to milliseconds
    # and nanoseconds should be retained by default
    expected = pa.Table.from_arrays([a_ms, a_ms, a_us, a_ns], names)
    _check_roundtrip(table, expected, version='2.0')

    # Using Parquet version 1.0, coercing to milliseconds or microseconds
    # is allowed
    expected = pa.Table.from_arrays([a_ms, a_ms, a_ms, a_ms], names)
    _check_roundtrip(table, expected, coerce_timestamps='ms')

    # Using Parquet version 2.0, coercing to milliseconds or microseconds
    # is allowed
    expected = pa.Table.from_arrays([a_us, a_us, a_us, a_us], names)
    _check_roundtrip(table, expected, version='2.0', coerce_timestamps='us')

    # TODO: after pyarrow allows coerce_timestamps='ns', tests like the
    # following should pass ...

    # Using Parquet version 1.0, coercing to nanoseconds is not allowed
    # expected = None
    # with pytest.raises(NotImplementedError):
    #     _roundtrip_table(table, coerce_timestamps='ns')

    # Using Parquet version 2.0, coercing to nanoseconds is allowed
    # expected = pa.Table.from_arrays([a_ns, a_ns, a_ns, a_ns], names)
    # _check_roundtrip(table, expected, version='2.0', coerce_timestamps='ns')

    # For either Parquet version, coercing to nanoseconds is allowed
    # if Int96 storage is used
    expected = pa.Table.from_arrays([a_ns, a_ns, a_ns, a_ns], names)
    _check_roundtrip(table, expected,
                     use_deprecated_int96_timestamps=True)
    _check_roundtrip(table, expected, version='2.0',
                     use_deprecated_int96_timestamps=True)


def test_large_list_records():
    # This was fixed in PARQUET-1100

    list_lengths = np.random.randint(0, 500, size=50)
    list_lengths[::10] = 0

    list_values = [list(map(int, np.random.randint(0, 100, size=x)))
                   if i % 8 else None
                   for i, x in enumerate(list_lengths)]

    a1 = pa.array(list_values)

    table = pa.Table.from_arrays([a1], ['int_lists'])
    _check_roundtrip(table)


def test_sanitized_spark_field_names():
    a0 = pa.array([0, 1, 2, 3, 4])
    name = 'prohib; ,\t{}'
    table = pa.Table.from_arrays([a0], [name])

    result = _roundtrip_table(table, write_table_kwargs={'flavor': 'spark'})

    expected_name = 'prohib______'
    assert result.schema[0].name == expected_name


@pytest.mark.pandas
def test_spark_flavor_preserves_pandas_metadata():
    df = _test_dataframe(size=100)
    df.index = np.arange(0, 10 * len(df), 10)
    df.index.name = 'foo'

    result = _roundtrip_pandas_dataframe(df, {'version': '2.0',
                                              'flavor': 'spark'})
    tm.assert_frame_equal(result, df)


def test_fixed_size_binary():
    t0 = pa.binary(10)
    data = [b'fooooooooo', None, b'barooooooo', b'quxooooooo']
    a0 = pa.array(data, type=t0)

    table = pa.Table.from_arrays([a0],
                                 ['binary[10]'])
    _check_roundtrip(table)


@pytest.mark.pandas
def test_multithreaded_read():
    df = alltypes_sample(size=10000)

    table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(table, buf, compression='SNAPPY', version='2.0')

    buf.seek(0)
    table1 = _read_table(buf, use_threads=True)

    buf.seek(0)
    table2 = _read_table(buf, use_threads=False)

    assert table1.equals(table2)


@pytest.mark.pandas
def test_min_chunksize():
    data = pd.DataFrame([np.arange(4)], columns=['A', 'B', 'C', 'D'])
    table = pa.Table.from_pandas(data.reset_index())

    buf = io.BytesIO()
    _write_table(table, buf, chunk_size=-1)

    buf.seek(0)
    result = _read_table(buf)

    assert result.equals(table)

    with pytest.raises(ValueError):
        _write_table(table, buf, chunk_size=0)


@pytest.mark.pandas
def test_pass_separate_metadata():
    # ARROW-471
    df = alltypes_sample(size=10000)

    a_table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(a_table, buf, compression='snappy', version='2.0')

    buf.seek(0)
    metadata = pq.read_metadata(buf)

    buf.seek(0)

    fileh = pq.ParquetFile(buf, metadata=metadata)

    tm.assert_frame_equal(df, fileh.read().to_pandas())


@pytest.mark.pandas
def test_read_single_row_group():
    # ARROW-471
    N, K = 10000, 4
    df = alltypes_sample(size=N)

    a_table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(a_table, buf, row_group_size=N / K,
                 compression='snappy', version='2.0')

    buf.seek(0)

    pf = pq.ParquetFile(buf)

    assert pf.num_row_groups == K

    row_groups = [pf.read_row_group(i) for i in range(K)]
    result = pa.concat_tables(row_groups)
    tm.assert_frame_equal(df, result.to_pandas())


@pytest.mark.pandas
def test_read_single_row_group_with_column_subset():
    N, K = 10000, 4
    df = alltypes_sample(size=N)
    a_table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(a_table, buf, row_group_size=N / K,
                 compression='snappy', version='2.0')

    buf.seek(0)
    pf = pq.ParquetFile(buf)

    cols = list(df.columns[:2])
    row_groups = [pf.read_row_group(i, columns=cols) for i in range(K)]
    result = pa.concat_tables(row_groups)
    tm.assert_frame_equal(df[cols], result.to_pandas())

    # ARROW-4267: Selection of duplicate columns still leads to these columns
    # being read uniquely.
    row_groups = [pf.read_row_group(i, columns=cols + cols) for i in range(K)]
    result = pa.concat_tables(row_groups)
    tm.assert_frame_equal(df[cols], result.to_pandas())


@pytest.mark.pandas
def test_read_multiple_row_groups():
    N, K = 10000, 4
    df = alltypes_sample(size=N)

    a_table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(a_table, buf, row_group_size=N / K,
                 compression='snappy', version='2.0')

    buf.seek(0)

    pf = pq.ParquetFile(buf)

    assert pf.num_row_groups == K

    result = pf.read_row_groups(range(K))
    tm.assert_frame_equal(df, result.to_pandas())


@pytest.mark.pandas
def test_read_multiple_row_groups_with_column_subset():
    N, K = 10000, 4
    df = alltypes_sample(size=N)
    a_table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(a_table, buf, row_group_size=N / K,
                 compression='snappy', version='2.0')

    buf.seek(0)
    pf = pq.ParquetFile(buf)

    cols = list(df.columns[:2])
    result = pf.read_row_groups(range(K), columns=cols)
    tm.assert_frame_equal(df[cols], result.to_pandas())

    # ARROW-4267: Selection of duplicate columns still leads to these columns
    # being read uniquely.
    result = pf.read_row_groups(range(K), columns=cols + cols)
    tm.assert_frame_equal(df[cols], result.to_pandas())


@pytest.mark.pandas
def test_scan_contents():
    N, K = 10000, 4
    df = alltypes_sample(size=N)
    a_table = pa.Table.from_pandas(df)

    buf = io.BytesIO()
    _write_table(a_table, buf, row_group_size=N / K,
                 compression='snappy', version='2.0')

    buf.seek(0)
    pf = pq.ParquetFile(buf)

    assert pf.scan_contents() == 10000
    assert pf.scan_contents(df.columns[:4]) == 10000


@pytest.mark.pandas
def test_parquet_piece_read(tempdir):
    df = _test_dataframe(1000)
    table = pa.Table.from_pandas(df)

    path = tempdir / 'parquet_piece_read.parquet'
    _write_table(table, path, version='2.0')

    piece1 = pq.ParquetDatasetPiece(path)

    result = piece1.read()
    assert result.equals(table)


@pytest.mark.pandas
def test_parquet_piece_open_and_get_metadata(tempdir):
    df = _test_dataframe(100)
    table = pa.Table.from_pandas(df)

    path = tempdir / 'parquet_piece_read.parquet'
    _write_table(table, path, version='2.0')

    piece = pq.ParquetDatasetPiece(path)
    table1 = piece.read()
    assert isinstance(table1, pa.Table)
    meta1 = piece.get_metadata()
    assert isinstance(meta1, pq.FileMetaData)

    assert table == table1


def test_parquet_piece_basics():
    path = '/baz.parq'

    piece1 = pq.ParquetDatasetPiece(path)
    piece2 = pq.ParquetDatasetPiece(path, row_group=1)
    piece3 = pq.ParquetDatasetPiece(
        path, row_group=1, partition_keys=[('foo', 0), ('bar', 1)])

    assert str(piece1) == path
    assert str(piece2) == '/baz.parq | row_group=1'
    assert str(piece3) == 'partition[foo=0, bar=1] /baz.parq | row_group=1'

    assert piece1 == piece1
    assert piece2 == piece2
    assert piece3 == piece3
    assert piece1 != piece3


def test_partition_set_dictionary_type():
    set1 = pq.PartitionSet('key1', [u('foo'), u('bar'), u('baz')])
    set2 = pq.PartitionSet('key2', [2007, 2008, 2009])

    assert isinstance(set1.dictionary, pa.StringArray)
    assert isinstance(set2.dictionary, pa.IntegerArray)

    set3 = pq.PartitionSet('key2', [datetime.datetime(2007, 1, 1)])
    with pytest.raises(TypeError):
        set3.dictionary


@pytest.mark.pandas
def test_read_partitioned_directory(tempdir):
    fs = LocalFileSystem.get_instance()
    _partition_test_for_filesystem(fs, tempdir)


@pytest.mark.pandas
def test_create_parquet_dataset_multi_threaded(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    _partition_test_for_filesystem(fs, base_path)

    manifest = pq.ParquetManifest(base_path, filesystem=fs,
                                  metadata_nthreads=1)
    dataset = pq.ParquetDataset(base_path, filesystem=fs, metadata_nthreads=16)
    assert len(dataset.pieces) > 0
    partitions = dataset.partitions
    assert len(partitions.partition_names) > 0
    assert partitions.partition_names == manifest.partitions.partition_names
    assert len(partitions.levels) == len(manifest.partitions.levels)


@pytest.mark.pandas
def test_equivalency(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    integer_keys = [0, 1]
    string_keys = ['a', 'b', 'c']
    boolean_keys = [True, False]
    partition_spec = [
        ['integer', integer_keys],
        ['string', string_keys],
        ['boolean', boolean_keys]
    ]

    df = pd.DataFrame({
        'integer': np.array(integer_keys, dtype='i4').repeat(15),
        'string': np.tile(np.tile(np.array(string_keys, dtype=object), 5), 2),
        'boolean': np.tile(np.tile(np.array(boolean_keys, dtype='bool'), 5),
                           3),
    }, columns=['integer', 'string', 'boolean'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    # Old filters syntax:
    #  integer == 1 AND string != b AND boolean == True
    dataset = pq.ParquetDataset(
        base_path, filesystem=fs,
        filters=[('integer', '=', 1), ('string', '!=', 'b'),
                 ('boolean', '==', True)]
    )
    table = dataset.read()
    result_df = (table.to_pandas().reset_index(drop=True))

    assert 0 not in result_df['integer'].values
    assert 'b' not in result_df['string'].values
    assert False not in result_df['boolean'].values

    # filters in disjunctive normal form:
    #  (integer == 1 AND string != b AND boolean == True) OR
    #  (integer == 2 AND boolean == False)
    # TODO(ARROW-3388): boolean columns are reconstructed as string
    filters = [
        [
            ('integer', '=', 1),
            ('string', '!=', 'b'),
            ('boolean', '==', 'True')
        ],
        [('integer', '=', 0), ('boolean', '==', 'False')]
    ]
    dataset = pq.ParquetDataset(base_path, filesystem=fs, filters=filters)
    table = dataset.read()
    result_df = table.to_pandas().reset_index(drop=True)

    # Check that all rows in the DF fulfill the filter
    # Pandas 0.23.x has problems with indexing constant memoryviews in
    # categoricals. Thus we need to make an explicity copy here with np.array.
    df_filter_1 = (np.array(result_df['integer']) == 1) \
        & (np.array(result_df['string']) != 'b') \
        & (np.array(result_df['boolean']) == 'True')
    df_filter_2 = (np.array(result_df['integer']) == 0) \
        & (np.array(result_df['boolean']) == 'False')
    assert df_filter_1.sum() > 0
    assert df_filter_2.sum() > 0
    assert result_df.shape[0] == (df_filter_1.sum() + df_filter_2.sum())

    # Check for \0 in predicate values. Until they are correctly implemented
    # in ARROW-3391, they would otherwise lead to weird results with the
    # current code.
    with pytest.raises(NotImplementedError):
        filters = [[('string', '==', b'1\0a')]]
        pq.ParquetDataset(base_path, filesystem=fs, filters=filters)
    with pytest.raises(NotImplementedError):
        filters = [[('string', '==', u'1\0a')]]
        pq.ParquetDataset(base_path, filesystem=fs, filters=filters)


@pytest.mark.pandas
def test_cutoff_exclusive_integer(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    integer_keys = [0, 1, 2, 3, 4]
    partition_spec = [
        ['integers', integer_keys],
    ]
    N = 5

    df = pd.DataFrame({
        'index': np.arange(N),
        'integers': np.array(integer_keys, dtype='i4'),
    }, columns=['index', 'integers'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    dataset = pq.ParquetDataset(
        base_path, filesystem=fs,
        filters=[
            ('integers', '<', 4),
            ('integers', '>', 1),
        ]
    )
    table = dataset.read()
    result_df = (table.to_pandas()
                      .sort_values(by='index')
                      .reset_index(drop=True))

    result_list = [x for x in map(int, result_df['integers'].values)]
    assert result_list == [2, 3]


@pytest.mark.pandas
@pytest.mark.xfail(
    raises=TypeError,
    reason='Loss of type information in creation of categoricals.'
)
def test_cutoff_exclusive_datetime(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    date_keys = [
        datetime.date(2018, 4, 9),
        datetime.date(2018, 4, 10),
        datetime.date(2018, 4, 11),
        datetime.date(2018, 4, 12),
        datetime.date(2018, 4, 13)
    ]
    partition_spec = [
        ['dates', date_keys]
    ]
    N = 5

    df = pd.DataFrame({
        'index': np.arange(N),
        'dates': np.array(date_keys, dtype='datetime64'),
    }, columns=['index', 'dates'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    dataset = pq.ParquetDataset(
        base_path, filesystem=fs,
        filters=[
            ('dates', '<', "2018-04-12"),
            ('dates', '>', "2018-04-10")
        ]
    )
    table = dataset.read()
    result_df = (table.to_pandas()
                      .sort_values(by='index')
                      .reset_index(drop=True))

    expected = pd.Categorical(
        np.array([datetime.date(2018, 4, 11)], dtype='datetime64'),
        categories=np.array(date_keys, dtype='datetime64'))

    assert result_df['dates'].values == expected


@pytest.mark.pandas
def test_inclusive_integer(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    integer_keys = [0, 1, 2, 3, 4]
    partition_spec = [
        ['integers', integer_keys],
    ]
    N = 5

    df = pd.DataFrame({
        'index': np.arange(N),
        'integers': np.array(integer_keys, dtype='i4'),
    }, columns=['index', 'integers'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    dataset = pq.ParquetDataset(
        base_path, filesystem=fs,
        filters=[
            ('integers', '<=', 3),
            ('integers', '>=', 2),
        ]
    )
    table = dataset.read()
    result_df = (table.to_pandas()
                 .sort_values(by='index')
                 .reset_index(drop=True))

    result_list = [int(x) for x in map(int, result_df['integers'].values)]
    assert result_list == [2, 3]


@pytest.mark.pandas
def test_inclusive_set(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    integer_keys = [0, 1]
    string_keys = ['a', 'b', 'c']
    boolean_keys = [True, False]
    partition_spec = [
        ['integer', integer_keys],
        ['string', string_keys],
        ['boolean', boolean_keys]
    ]

    df = pd.DataFrame({
        'integer': np.array(integer_keys, dtype='i4').repeat(15),
        'string': np.tile(np.tile(np.array(string_keys, dtype=object), 5), 2),
        'boolean': np.tile(np.tile(np.array(boolean_keys, dtype='bool'), 5),
                           3),
    }, columns=['integer', 'string', 'boolean'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    dataset = pq.ParquetDataset(
        base_path, filesystem=fs,
        filters=[('integer', 'in', {1}), ('string', 'in', {'a', 'b'}),
                 ('boolean', 'in', {True})]
    )
    table = dataset.read()
    result_df = (table.to_pandas().reset_index(drop=True))

    assert 0 not in result_df['integer'].values
    assert 'c' not in result_df['string'].values
    assert False not in result_df['boolean'].values


@pytest.mark.pandas
def test_invalid_pred_op(tempdir):
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    integer_keys = [0, 1, 2, 3, 4]
    partition_spec = [
        ['integers', integer_keys],
    ]
    N = 5

    df = pd.DataFrame({
        'index': np.arange(N),
        'integers': np.array(integer_keys, dtype='i4'),
    }, columns=['index', 'integers'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    with pytest.raises(ValueError):
        pq.ParquetDataset(base_path,
                          filesystem=fs,
                          filters=[
                            ('integers', '=<', 3),
                          ])

    with pytest.raises(ValueError):
        pq.ParquetDataset(base_path,
                          filesystem=fs,
                          filters=[
                            ('integers', 'in', set()),
                          ])

    with pytest.raises(ValueError):
        pq.ParquetDataset(base_path,
                          filesystem=fs,
                          filters=[
                            ('integers', '!=', {3}),
                          ])


@pytest.mark.pandas
def test_filters_read_table(tempdir):
    # test that filters keyword is passed through in read_table
    fs = LocalFileSystem.get_instance()
    base_path = tempdir

    integer_keys = [0, 1, 2, 3, 4]
    partition_spec = [
        ['integers', integer_keys],
    ]
    N = 5

    df = pd.DataFrame({
        'index': np.arange(N),
        'integers': np.array(integer_keys, dtype='i4'),
    }, columns=['index', 'integers'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    table = pq.read_table(
        base_path, filesystem=fs, filters=[('integers', '<', 3)])
    assert table.num_rows == 3

    table = pq.read_table(
        base_path, filesystem=fs, filters=[[('integers', '<', 3)]])
    assert table.num_rows == 3

    table = pq.read_pandas(
        base_path, filters=[('integers', '<', 3)])
    assert table.num_rows == 3


@pytest.yield_fixture
def s3_example():
    access_key = os.environ['PYARROW_TEST_S3_ACCESS_KEY']
    secret_key = os.environ['PYARROW_TEST_S3_SECRET_KEY']
    bucket_name = os.environ['PYARROW_TEST_S3_BUCKET']

    import s3fs
    fs = s3fs.S3FileSystem(key=access_key, secret=secret_key)

    test_dir = guid()

    bucket_uri = 's3://{0}/{1}'.format(bucket_name, test_dir)
    fs.mkdir(bucket_uri)
    yield fs, bucket_uri
    fs.rm(bucket_uri, recursive=True)


@pytest.mark.pandas
@pytest.mark.s3
def test_read_partitioned_directory_s3fs(s3_example):
    from pyarrow.filesystem import S3FSWrapper

    fs, bucket_uri = s3_example
    wrapper = S3FSWrapper(fs)
    _partition_test_for_filesystem(wrapper, bucket_uri)

    # Check that we can auto-wrap
    dataset = pq.ParquetDataset(bucket_uri, filesystem=fs)
    dataset.read()


def _partition_test_for_filesystem(fs, base_path):
    foo_keys = [0, 1]
    bar_keys = ['a', 'b', 'c']
    partition_spec = [
        ['foo', foo_keys],
        ['bar', bar_keys]
    ]
    N = 30

    df = pd.DataFrame({
        'index': np.arange(N),
        'foo': np.array(foo_keys, dtype='i4').repeat(15),
        'bar': np.tile(np.tile(np.array(bar_keys, dtype=object), 5), 2),
        'values': np.random.randn(N)
    }, columns=['index', 'foo', 'bar', 'values'])

    _generate_partition_directories(fs, base_path, partition_spec, df)

    dataset = pq.ParquetDataset(base_path, filesystem=fs)
    table = dataset.read()
    result_df = (table.to_pandas()
                 .sort_values(by='index')
                 .reset_index(drop=True))

    expected_df = (df.sort_values(by='index')
                   .reset_index(drop=True)
                   .reindex(columns=result_df.columns))
    expected_df['foo'] = pd.Categorical(df['foo'], categories=foo_keys)
    expected_df['bar'] = pd.Categorical(df['bar'], categories=bar_keys)

    assert (result_df.columns == ['index', 'values', 'foo', 'bar']).all()

    tm.assert_frame_equal(result_df, expected_df)


def _generate_partition_directories(fs, base_dir, partition_spec, df):
    # partition_spec : list of lists, e.g. [['foo', [0, 1, 2],
    #                                       ['bar', ['a', 'b', 'c']]
    # part_table : a pyarrow.Table to write to each partition
    DEPTH = len(partition_spec)

    def _visit_level(base_dir, level, part_keys):
        name, values = partition_spec[level]
        for value in values:
            this_part_keys = part_keys + [(name, value)]

            level_dir = base_dir / '{0}={1}'.format(name, value)
            fs.mkdir(level_dir)

            if level == DEPTH - 1:
                # Generate example data
                file_path = level_dir / guid()

                filtered_df = _filter_partition(df, this_part_keys)
                part_table = pa.Table.from_pandas(filtered_df)
                with fs.open(file_path, 'wb') as f:
                    _write_table(part_table, f)
                assert fs.exists(file_path)

                (level_dir / '_SUCCESS').touch()
            else:
                _visit_level(level_dir, level + 1, this_part_keys)
                (level_dir / '_SUCCESS').touch()

    _visit_level(base_dir, 0, [])


def _test_read_common_metadata_files(fs, base_path):
    N = 100
    df = pd.DataFrame({
        'index': np.arange(N),
        'values': np.random.randn(N)
    }, columns=['index', 'values'])

    base_path = str(base_path)
    data_path = os.path.join(base_path, 'data.parquet')

    table = pa.Table.from_pandas(df)

    with fs.open(data_path, 'wb') as f:
        _write_table(table, f)

    metadata_path = os.path.join(base_path, '_common_metadata')
    with fs.open(metadata_path, 'wb') as f:
        pq.write_metadata(table.schema, f)

    dataset = pq.ParquetDataset(base_path, filesystem=fs)
    assert dataset.common_metadata_path == str(metadata_path)

    with fs.open(data_path) as f:
        common_schema = pq.read_metadata(f).schema
    assert dataset.schema.equals(common_schema)

    # handle list of one directory
    dataset2 = pq.ParquetDataset([base_path], filesystem=fs)
    assert dataset2.schema.equals(dataset.schema)


@pytest.mark.pandas
def test_read_common_metadata_files(tempdir):
    fs = LocalFileSystem.get_instance()
    _test_read_common_metadata_files(fs, tempdir)


@pytest.mark.pandas
def test_read_metadata_files(tempdir):
    fs = LocalFileSystem.get_instance()

    N = 100
    df = pd.DataFrame({
        'index': np.arange(N),
        'values': np.random.randn(N)
    }, columns=['index', 'values'])

    data_path = tempdir / 'data.parquet'

    table = pa.Table.from_pandas(df)

    with fs.open(data_path, 'wb') as f:
        _write_table(table, f)

    metadata_path = tempdir / '_metadata'
    with fs.open(metadata_path, 'wb') as f:
        pq.write_metadata(table.schema, f)

    dataset = pq.ParquetDataset(tempdir, filesystem=fs)
    assert dataset.metadata_path == str(metadata_path)

    with fs.open(data_path) as f:
        metadata_schema = pq.read_metadata(f).schema
    assert dataset.schema.equals(metadata_schema)


@pytest.mark.pandas
def test_read_schema(tempdir):
    N = 100
    df = pd.DataFrame({
        'index': np.arange(N),
        'values': np.random.randn(N)
    }, columns=['index', 'values'])

    data_path = tempdir / 'test.parquet'

    table = pa.Table.from_pandas(df)
    _write_table(table, data_path)

    read1 = pq.read_schema(data_path)
    read2 = pq.read_schema(data_path, memory_map=True)
    assert table.schema.equals(read1, check_metadata=False)
    assert table.schema.equals(read2, check_metadata=False)

    assert table.schema.metadata[b'pandas'] == read1.metadata[b'pandas']


def _filter_partition(df, part_keys):
    predicate = np.ones(len(df), dtype=bool)

    to_drop = []
    for name, value in part_keys:
        to_drop.append(name)

        # to avoid pandas warning
        if isinstance(value, (datetime.date, datetime.datetime)):
            value = pd.Timestamp(value)

        predicate &= df[name] == value

    return df[predicate].drop(to_drop, axis=1)


@pytest.mark.pandas
def test_read_multiple_files(tempdir):
    nfiles = 10
    size = 5

    dirpath = tempdir / guid()
    dirpath.mkdir()

    test_data = []
    paths = []
    for i in range(nfiles):
        df = _test_dataframe(size, seed=i)

        # Hack so that we don't have a dtype cast in v1 files
        df['uint32'] = df['uint32'].astype(np.int64)

        path = dirpath / '{}.parquet'.format(i)

        table = pa.Table.from_pandas(df)
        _write_table(table, path)

        test_data.append(table)
        paths.append(path)

    # Write a _SUCCESS.crc file
    (dirpath / '_SUCCESS.crc').touch()

    def read_multiple_files(paths, columns=None, use_threads=True, **kwargs):
        dataset = pq.ParquetDataset(paths, **kwargs)
        return dataset.read(columns=columns, use_threads=use_threads)

    result = read_multiple_files(paths)
    expected = pa.concat_tables(test_data)

    assert result.equals(expected)

    # Read with provided metadata
    metadata = pq.read_metadata(paths[0])

    result2 = read_multiple_files(paths, metadata=metadata)
    assert result2.equals(expected)

    result3 = pa.localfs.read_parquet(dirpath, schema=metadata.schema)
    assert result3.equals(expected)

    # Read column subset
    to_read = [0, 2, 6, result.num_columns - 1]

    col_names = [result.field(i).name for i in to_read]
    out = pa.localfs.read_parquet(dirpath, columns=col_names)
    expected = pa.Table.from_arrays([result.column(i) for i in to_read],
                                    names=col_names,
                                    metadata=result.schema.metadata)
    assert out.equals(expected)

    # Read with multiple threads
    pa.localfs.read_parquet(dirpath, use_threads=True)

    # Test failure modes with non-uniform metadata
    bad_apple = _test_dataframe(size, seed=i).iloc[:, :4]
    bad_apple_path = tempdir / '{}.parquet'.format(guid())

    t = pa.Table.from_pandas(bad_apple)
    _write_table(t, bad_apple_path)

    bad_meta = pq.read_metadata(bad_apple_path)

    with pytest.raises(ValueError):
        read_multiple_files(paths + [bad_apple_path])

    with pytest.raises(ValueError):
        read_multiple_files(paths, metadata=bad_meta)

    mixed_paths = [bad_apple_path, paths[0]]

    with pytest.raises(ValueError):
        read_multiple_files(mixed_paths, schema=bad_meta.schema)

    with pytest.raises(ValueError):
        read_multiple_files(mixed_paths)


@pytest.mark.pandas
def test_dataset_read_pandas(tempdir):
    nfiles = 5
    size = 5

    dirpath = tempdir / guid()
    dirpath.mkdir()

    test_data = []
    frames = []
    paths = []
    for i in range(nfiles):
        df = _test_dataframe(size, seed=i)
        df.index = np.arange(i * size, (i + 1) * size)
        df.index.name = 'index'

        path = dirpath / '{}.parquet'.format(i)

        table = pa.Table.from_pandas(df)
        _write_table(table, path)
        test_data.append(table)
        frames.append(df)
        paths.append(path)

    dataset = pq.ParquetDataset(dirpath)
    columns = ['uint8', 'strings']
    result = dataset.read_pandas(columns=columns).to_pandas()
    expected = pd.concat([x[columns] for x in frames])

    tm.assert_frame_equal(result, expected)


@pytest.mark.pandas
def test_dataset_no_memory_map(tempdir):
    # ARROW-2627: Check that we can use ParquetDataset without memory-mapping
    dirpath = tempdir / guid()
    dirpath.mkdir()

    df = _test_dataframe(10, seed=0)
    path = dirpath / '{}.parquet'.format(0)
    table = pa.Table.from_pandas(df)
    _write_table(table, path, version='2.0')

    # TODO(wesm): Not sure how to easily check that memory mapping is _not_
    # used. Mocking is not especially easy for pa.memory_map
    dataset = pq.ParquetDataset(dirpath, memory_map=False)
    assert dataset.pieces[0].read().equals(table)


@pytest.mark.pandas
@pytest.mark.parametrize('preserve_index', [True, False, None])
def test_dataset_read_pandas_common_metadata(tempdir, preserve_index):
    # ARROW-1103
    nfiles = 5
    size = 5

    dirpath = tempdir / guid()
    dirpath.mkdir()

    test_data = []
    frames = []
    paths = []
    for i in range(nfiles):
        df = _test_dataframe(size, seed=i)
        df.index = pd.Index(np.arange(i * size, (i + 1) * size), name='index')

        path = dirpath / '{}.parquet'.format(i)

        table = pa.Table.from_pandas(df, preserve_index=preserve_index)

        # Obliterate metadata
        table = table.replace_schema_metadata(None)
        assert table.schema.metadata is None

        _write_table(table, path)
        test_data.append(table)
        frames.append(df)
        paths.append(path)

    # Write _metadata common file
    table_for_metadata = pa.Table.from_pandas(
        df, preserve_index=preserve_index
    )
    pq.write_metadata(table_for_metadata.schema, dirpath / '_metadata')

    dataset = pq.ParquetDataset(dirpath)
    columns = ['uint8', 'strings']
    result = dataset.read_pandas(columns=columns).to_pandas()
    expected = pd.concat([x[columns] for x in frames])
    expected.index.name = (
        df.index.name if preserve_index is not False else None)
    tm.assert_frame_equal(result, expected)


def _make_example_multifile_dataset(base_path, nfiles=10, file_nrows=5):
    test_data = []
    paths = []
    for i in range(nfiles):
        df = _test_dataframe(file_nrows, seed=i)
        path = base_path / '{}.parquet'.format(i)

        test_data.append(_write_table(df, path))
        paths.append(path)
    return paths


@pytest.mark.pandas
def test_ignore_private_directories(tempdir):
    dirpath = tempdir / guid()
    dirpath.mkdir()

    paths = _make_example_multifile_dataset(dirpath, nfiles=10,
                                            file_nrows=5)

    # private directory
    (dirpath / '_impala_staging').mkdir()

    dataset = pq.ParquetDataset(dirpath)
    assert set(map(str, paths)) == set(x.path for x in dataset.pieces)


@pytest.mark.pandas
def test_ignore_hidden_files_dot(tempdir):
    dirpath = tempdir / guid()
    dirpath.mkdir()

    paths = _make_example_multifile_dataset(dirpath, nfiles=10,
                                            file_nrows=5)

    with (dirpath / '.DS_Store').open('wb') as f:
        f.write(b'gibberish')

    with (dirpath / '.private').open('wb') as f:
        f.write(b'gibberish')

    dataset = pq.ParquetDataset(dirpath)
    assert set(map(str, paths)) == set(x.path for x in dataset.pieces)


@pytest.mark.pandas
def test_ignore_hidden_files_underscore(tempdir):
    dirpath = tempdir / guid()
    dirpath.mkdir()

    paths = _make_example_multifile_dataset(dirpath, nfiles=10,
                                            file_nrows=5)

    with (dirpath / '_committed_123').open('wb') as f:
        f.write(b'abcd')

    with (dirpath / '_started_321').open('wb') as f:
        f.write(b'abcd')

    dataset = pq.ParquetDataset(dirpath)
    assert set(map(str, paths)) == set(x.path for x in dataset.pieces)


@pytest.mark.pandas
def test_multiindex_duplicate_values(tempdir):
    num_rows = 3
    numbers = list(range(num_rows))
    index = pd.MultiIndex.from_arrays(
        [['foo', 'foo', 'bar'], numbers],
        names=['foobar', 'some_numbers'],
    )

    df = pd.DataFrame({'numbers': numbers}, index=index)
    table = pa.Table.from_pandas(df)

    filename = tempdir / 'dup_multi_index_levels.parquet'

    _write_table(table, filename)
    result_table = _read_table(filename)
    assert table.equals(result_table)

    result_df = result_table.to_pandas()
    tm.assert_frame_equal(result_df, df)


@pytest.mark.pandas
def test_write_error_deletes_incomplete_file(tempdir):
    # ARROW-1285
    df = pd.DataFrame({'a': list('abc'),
                       'b': list(range(1, 4)),
                       'c': np.arange(3, 6).astype('u1'),
                       'd': np.arange(4.0, 7.0, dtype='float64'),
                       'e': [True, False, True],
                       'f': pd.Categorical(list('abc')),
                       'g': pd.date_range('20130101', periods=3),
                       'h': pd.date_range('20130101', periods=3,
                                          tz='US/Eastern'),
                       'i': pd.date_range('20130101', periods=3, freq='ns')})

    pdf = pa.Table.from_pandas(df)

    filename = tempdir / 'tmp_file'
    try:
        _write_table(pdf, filename)
    except pa.ArrowException:
        pass

    assert not filename.exists()


@pytest.mark.pandas
def test_noncoerced_nanoseconds_written_without_exception(tempdir):
    # ARROW-1957: the Parquet version 2.0 writer preserves Arrow
    # nanosecond timestamps by default
    n = 9
    df = pd.DataFrame({'x': range(n)},
                      index=pd.DatetimeIndex(start='2017-01-01',
                      freq='1n',
                      periods=n))
    tb = pa.Table.from_pandas(df)

    filename = tempdir / 'written.parquet'
    try:
        pq.write_table(tb, filename, version='2.0')
    except Exception:
        pass
    assert filename.exists()

    recovered_table = pq.read_table(filename)
    assert tb.equals(recovered_table)

    # Loss of data thru coercion (without explicit override) still an error
    filename = tempdir / 'not_written.parquet'
    with pytest.raises(ValueError):
        pq.write_table(tb, filename, coerce_timestamps='ms', version='2.0')


def test_read_non_existent_file(tempdir):
    path = 'non-existent-file.parquet'
    try:
        pq.read_table(path)
    except Exception as e:
        assert path in e.args[0]


def test_read_table_doesnt_warn(datadir):
    with pytest.warns(None) as record:
        pq.read_table(datadir / 'v0.7.1.parquet')

    assert len(record) == 0


def _test_write_to_dataset_with_partitions(base_path,
                                           filesystem=None,
                                           schema=None,
                                           index_name=None):
    # ARROW-1400
    output_df = pd.DataFrame({'group1': list('aaabbbbccc'),
                              'group2': list('eefeffgeee'),
                              'num': list(range(10)),
                              'nan': [pd.np.nan] * 10,
                              'date': np.arange('2017-01-01', '2017-01-11',
                                                dtype='datetime64[D]')})
    cols = output_df.columns.tolist()
    partition_by = ['group1', 'group2']
    output_table = pa.Table.from_pandas(output_df, schema=schema, safe=False,
                                        preserve_index=False)
    pq.write_to_dataset(output_table, base_path, partition_by,
                        filesystem=filesystem)

    metadata_path = os.path.join(base_path, '_common_metadata')

    if filesystem is not None:
        with filesystem.open(metadata_path, 'wb') as f:
            pq.write_metadata(output_table.schema, f)
    else:
        pq.write_metadata(output_table.schema, metadata_path)

    # ARROW-2891: Ensure the output_schema is preserved when writing a
    # partitioned dataset
    dataset = pq.ParquetDataset(base_path,
                                filesystem=filesystem,
                                validate_schema=True)
    # ARROW-2209: Ensure the dataset schema also includes the partition columns
    dataset_cols = set(dataset.schema.to_arrow_schema().names)
    assert dataset_cols == set(output_table.schema.names)

    input_table = dataset.read()
    input_df = input_table.to_pandas()

    # Read data back in and compare with original DataFrame
    # Partitioned columns added to the end of the DataFrame when read
    input_df_cols = input_df.columns.tolist()
    assert partition_by == input_df_cols[-1 * len(partition_by):]

    # Partitioned columns become 'categorical' dtypes
    input_df = input_df[cols]
    for col in partition_by:
        output_df[col] = output_df[col].astype('category')
    assert output_df.equals(input_df)


def _test_write_to_dataset_no_partitions(base_path, filesystem=None):
    # ARROW-1400
    output_df = pd.DataFrame({'group1': list('aaabbbbccc'),
                              'group2': list('eefeffgeee'),
                              'num': list(range(10)),
                              'date': np.arange('2017-01-01', '2017-01-11',
                                                dtype='datetime64[D]')})
    cols = output_df.columns.tolist()
    output_table = pa.Table.from_pandas(output_df)

    if filesystem is None:
        filesystem = LocalFileSystem.get_instance()

    # Without partitions, append files to root_path
    n = 5
    for i in range(n):
        pq.write_to_dataset(output_table, base_path,
                            filesystem=filesystem)
    output_files = [file for file in filesystem.ls(base_path)
                    if file.endswith(".parquet")]
    assert len(output_files) == n

    # Deduplicated incoming DataFrame should match
    # original outgoing Dataframe
    input_table = pq.ParquetDataset(base_path,
                                    filesystem=filesystem).read()
    input_df = input_table.to_pandas()
    input_df = input_df.drop_duplicates()
    input_df = input_df[cols]
    assert output_df.equals(input_df)


@pytest.mark.pandas
def test_write_to_dataset_with_partitions(tempdir):
    _test_write_to_dataset_with_partitions(str(tempdir))


@pytest.mark.pandas
def test_write_to_dataset_with_partitions_and_schema(tempdir):
    schema = pa.schema([pa.field('group1', type=pa.string()),
                        pa.field('group2', type=pa.string()),
                        pa.field('num', type=pa.int64()),
                        pa.field('nan', type=pa.int32()),
                        pa.field('date', type=pa.timestamp(unit='us'))])
    _test_write_to_dataset_with_partitions(str(tempdir), schema=schema)


@pytest.mark.pandas
def test_write_to_dataset_with_partitions_and_index_name(tempdir):
    _test_write_to_dataset_with_partitions(str(tempdir),
                                           index_name='index_name')


@pytest.mark.pandas
def test_write_to_dataset_no_partitions(tempdir):
    _test_write_to_dataset_no_partitions(str(tempdir))


@pytest.mark.pandas
def test_write_to_dataset_with_partitions_and_custom_filenames(tempdir):
    output_df = pd.DataFrame({'group1': list('aaabbbbccc'),
                              'group2': list('eefeffgeee'),
                              'num': list(range(10)),
                              'nan': [pd.np.nan] * 10,
                              'date': np.arange('2017-01-01', '2017-01-11',
                                                dtype='datetime64[D]')})
    partition_by = ['group1', 'group2']
    output_table = pa.Table.from_pandas(output_df)
    path = str(tempdir)

    def partition_filename_callback(keys):
        return "{0}-{1}.parquet".format(*keys)

    pq.write_to_dataset(output_table, path,
                        partition_by, partition_filename_callback)

    dataset = pq.ParquetDataset(path)

    # ARROW-3538: Ensure partition filenames match the given pattern
    # defined in the local function partition_filename_callback
    expected_basenames = [
        'a-e.parquet', 'a-f.parquet',
        'b-e.parquet', 'b-f.parquet',
        'b-g.parquet', 'c-e.parquet'
    ]
    output_basenames = [os.path.basename(p.path) for p in dataset.pieces]

    assert sorted(expected_basenames) == sorted(output_basenames)


@pytest.mark.large_memory
def test_large_table_int32_overflow():
    size = np.iinfo('int32').max + 1

    arr = np.ones(size, dtype='uint8')

    parr = pa.array(arr, type=pa.uint8())

    table = pa.Table.from_arrays([parr], names=['one'])
    f = io.BytesIO()
    _write_table(table, f)


def _simple_table_roundtrip(table):
    stream = pa.BufferOutputStream()
    _write_table(table, stream)
    buf = stream.getvalue()
    return _read_table(buf)


@pytest.mark.pandas
@pytest.mark.large_memory
def test_binary_array_overflow_to_chunked():
    # ARROW-3762

    # 2^31 + 1 bytes
    values = [b'x'] + [
        b'x' * (1 << 20)
    ] * 2 * (1 << 10)
    df = pd.DataFrame({'byte_col': values})

    tbl = pa.Table.from_pandas(df, preserve_index=False)
    read_tbl = _simple_table_roundtrip(tbl)

    col0_data = read_tbl[0]
    assert isinstance(col0_data, pa.ChunkedArray)

    # Split up into 2GB chunks
    assert col0_data.num_chunks == 2

    assert tbl.equals(read_tbl)


@pytest.mark.pandas
@pytest.mark.large_memory
def test_list_of_binary_large_cell():
    # ARROW-4688
    data = []

    # TODO(wesm): handle chunked children
    # 2^31 - 1 bytes in a single cell
    # data.append([b'x' * (1 << 20)] * 2047 + [b'x' * ((1 << 20) - 1)])

    # A little under 2GB in cell each containing approximately 10MB each
    data.extend([[b'x' * 1000000] * 10] * 214)

    arr = pa.array(data)
    table = pa.Table.from_arrays([arr], ['chunky_cells'])
    read_table = _simple_table_roundtrip(table)
    assert table.equals(read_table)


@pytest.mark.pandas
def test_index_column_name_duplicate(tempdir):
    data = {
        'close': {
            pd.Timestamp('2017-06-30 01:31:00'): 154.99958999999998,
            pd.Timestamp('2017-06-30 01:32:00'): 154.99958999999998,
        },
        'time': {
            pd.Timestamp('2017-06-30 01:31:00'): pd.Timestamp(
                '2017-06-30 01:31:00'
            ),
            pd.Timestamp('2017-06-30 01:32:00'): pd.Timestamp(
                '2017-06-30 01:32:00'
            ),
        }
    }
    path = str(tempdir / 'data.parquet')
    dfx = pd.DataFrame(data).set_index('time', drop=False)
    tdfx = pa.Table.from_pandas(dfx)
    _write_table(tdfx, path)
    arrow_table = _read_table(path)
    result_df = arrow_table.to_pandas()
    tm.assert_frame_equal(result_df, dfx)


@pytest.mark.pandas
def test_parquet_nested_convenience(tempdir):
    # ARROW-1684
    df = pd.DataFrame({
        'a': [[1, 2, 3], None, [4, 5], []],
        'b': [[1.], None, None, [6., 7.]],
    })

    path = str(tempdir / 'nested_convenience.parquet')

    table = pa.Table.from_pandas(df, preserve_index=False)
    _write_table(table, path)

    read = pq.read_table(path, columns=['a'])
    tm.assert_frame_equal(read.to_pandas(), df[['a']])

    read = pq.read_table(path, columns=['a', 'b'])
    tm.assert_frame_equal(read.to_pandas(), df)


@pytest.mark.pandas
def test_backwards_compatible_index_naming(datadir):
    expected_string = b"""\
carat        cut  color  clarity  depth  table  price     x     y     z
 0.23      Ideal      E      SI2   61.5   55.0    326  3.95  3.98  2.43
 0.21    Premium      E      SI1   59.8   61.0    326  3.89  3.84  2.31
 0.23       Good      E      VS1   56.9   65.0    327  4.05  4.07  2.31
 0.29    Premium      I      VS2   62.4   58.0    334  4.20  4.23  2.63
 0.31       Good      J      SI2   63.3   58.0    335  4.34  4.35  2.75
 0.24  Very Good      J     VVS2   62.8   57.0    336  3.94  3.96  2.48
 0.24  Very Good      I     VVS1   62.3   57.0    336  3.95  3.98  2.47
 0.26  Very Good      H      SI1   61.9   55.0    337  4.07  4.11  2.53
 0.22       Fair      E      VS2   65.1   61.0    337  3.87  3.78  2.49
 0.23  Very Good      H      VS1   59.4   61.0    338  4.00  4.05  2.39"""
    expected = pd.read_csv(io.BytesIO(expected_string), sep=r'\s{2,}',
                           index_col=None, header=0, engine='python')
    table = _read_table(datadir / 'v0.7.1.parquet')
    result = table.to_pandas()
    tm.assert_frame_equal(result, expected)


@pytest.mark.pandas
def test_backwards_compatible_index_multi_level_named(datadir):
    expected_string = b"""\
carat        cut  color  clarity  depth  table  price     x     y     z
 0.23      Ideal      E      SI2   61.5   55.0    326  3.95  3.98  2.43
 0.21    Premium      E      SI1   59.8   61.0    326  3.89  3.84  2.31
 0.23       Good      E      VS1   56.9   65.0    327  4.05  4.07  2.31
 0.29    Premium      I      VS2   62.4   58.0    334  4.20  4.23  2.63
 0.31       Good      J      SI2   63.3   58.0    335  4.34  4.35  2.75
 0.24  Very Good      J     VVS2   62.8   57.0    336  3.94  3.96  2.48
 0.24  Very Good      I     VVS1   62.3   57.0    336  3.95  3.98  2.47
 0.26  Very Good      H      SI1   61.9   55.0    337  4.07  4.11  2.53
 0.22       Fair      E      VS2   65.1   61.0    337  3.87  3.78  2.49
 0.23  Very Good      H      VS1   59.4   61.0    338  4.00  4.05  2.39"""
    expected = pd.read_csv(
        io.BytesIO(expected_string), sep=r'\s{2,}',
        index_col=['cut', 'color', 'clarity'],
        header=0, engine='python'
    ).sort_index()

    table = _read_table(datadir / 'v0.7.1.all-named-index.parquet')
    result = table.to_pandas()
    tm.assert_frame_equal(result, expected)


@pytest.mark.pandas
def test_backwards_compatible_index_multi_level_some_named(datadir):
    expected_string = b"""\
carat        cut  color  clarity  depth  table  price     x     y     z
 0.23      Ideal      E      SI2   61.5   55.0    326  3.95  3.98  2.43
 0.21    Premium      E      SI1   59.8   61.0    326  3.89  3.84  2.31
 0.23       Good      E      VS1   56.9   65.0    327  4.05  4.07  2.31
 0.29    Premium      I      VS2   62.4   58.0    334  4.20  4.23  2.63
 0.31       Good      J      SI2   63.3   58.0    335  4.34  4.35  2.75
 0.24  Very Good      J     VVS2   62.8   57.0    336  3.94  3.96  2.48
 0.24  Very Good      I     VVS1   62.3   57.0    336  3.95  3.98  2.47
 0.26  Very Good      H      SI1   61.9   55.0    337  4.07  4.11  2.53
 0.22       Fair      E      VS2   65.1   61.0    337  3.87  3.78  2.49
 0.23  Very Good      H      VS1   59.4   61.0    338  4.00  4.05  2.39"""
    expected = pd.read_csv(
        io.BytesIO(expected_string),
        sep=r'\s{2,}', index_col=['cut', 'color', 'clarity'],
        header=0, engine='python'
    ).sort_index()
    expected.index = expected.index.set_names(['cut', None, 'clarity'])

    table = _read_table(datadir / 'v0.7.1.some-named-index.parquet')
    result = table.to_pandas()
    tm.assert_frame_equal(result, expected)


@pytest.mark.pandas
def test_backwards_compatible_column_metadata_handling(datadir):
    expected = pd.DataFrame(
        {'a': [1, 2, 3], 'b': [.1, .2, .3],
         'c': pd.date_range("2017-01-01", periods=3, tz='Europe/Brussels')})
    expected.index = pd.MultiIndex.from_arrays(
        [['a', 'b', 'c'],
         pd.date_range("2017-01-01", periods=3, tz='Europe/Brussels')],
        names=['index', None])

    path = datadir / 'v0.7.1.column-metadata-handling.parquet'
    table = _read_table(path)
    result = table.to_pandas()
    tm.assert_frame_equal(result, expected)

    table = _read_table(path, columns=['a'])
    result = table.to_pandas()
    tm.assert_frame_equal(result, expected[['a']].reset_index(drop=True))


def _make_dataset_for_pickling(tempdir, N=100):
    path = tempdir / 'data.parquet'
    fs = LocalFileSystem.get_instance()

    df = pd.DataFrame({
        'index': np.arange(N),
        'values': np.random.randn(N)
    }, columns=['index', 'values'])
    table = pa.Table.from_pandas(df)

    num_groups = 3
    with pq.ParquetWriter(path, table.schema) as writer:
        for i in range(num_groups):
            writer.write_table(table)

    reader = pq.ParquetFile(path)
    assert reader.metadata.num_row_groups == num_groups

    metadata_path = tempdir / '_metadata'
    with fs.open(metadata_path, 'wb') as f:
        pq.write_metadata(table.schema, f)

    dataset = pq.ParquetDataset(tempdir, filesystem=fs)
    assert dataset.metadata_path == str(metadata_path)

    return dataset


@pytest.mark.pandas
@pytest.mark.parametrize('pickler', [
    pytest.param(pickle, id='builtin'),
    pytest.param(pytest.importorskip('cloudpickle'), id='cloudpickle')
])
def test_pickle_dataset(tempdir, datadir, pickler):
    def is_pickleable(obj):
        return obj == pickler.loads(pickler.dumps(obj))

    dataset = _make_dataset_for_pickling(tempdir)

    assert is_pickleable(dataset)
    assert is_pickleable(dataset.metadata)
    assert is_pickleable(dataset.metadata.schema)
    assert len(dataset.metadata.schema)
    for column in dataset.metadata.schema:
        assert is_pickleable(column)

    for piece in dataset.pieces:
        assert is_pickleable(piece)
        metadata = piece.get_metadata()
        assert metadata.num_row_groups
        for i in range(metadata.num_row_groups):
            assert is_pickleable(metadata.row_group(i))


@pytest.mark.pandas
def test_decimal_roundtrip(tempdir):
    num_values = 10

    columns = {}
    for precision in range(1, 39):
        for scale in range(0, precision + 1):
            with util.random_seed(0):
                random_decimal_values = [
                    util.randdecimal(precision, scale)
                    for _ in range(num_values)
                ]
            column_name = ('dec_precision_{:d}_scale_{:d}'
                           .format(precision, scale))
            columns[column_name] = random_decimal_values

    expected = pd.DataFrame(columns)
    filename = tempdir / 'decimals.parquet'
    string_filename = str(filename)
    table = pa.Table.from_pandas(expected)
    _write_table(table, string_filename)
    result_table = _read_table(string_filename)
    result = result_table.to_pandas()
    tm.assert_frame_equal(result, expected)


@pytest.mark.pandas
@pytest.mark.xfail(
    raises=pa.ArrowException, reason='Parquet does not support negative scale'
)
def test_decimal_roundtrip_negative_scale(tempdir):
    expected = pd.DataFrame({'decimal_num': [decimal.Decimal('1.23E4')]})
    filename = tempdir / 'decimals.parquet'
    string_filename = str(filename)
    t = pa.Table.from_pandas(expected)
    _write_table(t, string_filename)
    result_table = _read_table(string_filename)
    result = result_table.to_pandas()
    tm.assert_frame_equal(result, expected)


@pytest.mark.pandas
def test_parquet_writer_context_obj(tempdir):
    df = _test_dataframe(100)
    df['unique_id'] = 0

    arrow_table = pa.Table.from_pandas(df, preserve_index=False)
    out = pa.BufferOutputStream()

    with pq.ParquetWriter(out, arrow_table.schema, version='2.0') as writer:

        frames = []
        for i in range(10):
            df['unique_id'] = i
            arrow_table = pa.Table.from_pandas(df, preserve_index=False)
            writer.write_table(arrow_table)

            frames.append(df.copy())

    buf = out.getvalue()
    result = _read_table(pa.BufferReader(buf))

    expected = pd.concat(frames, ignore_index=True)
    tm.assert_frame_equal(result.to_pandas(), expected)


@pytest.mark.pandas
def test_parquet_writer_context_obj_with_exception(tempdir):
    df = _test_dataframe(100)
    df['unique_id'] = 0

    arrow_table = pa.Table.from_pandas(df, preserve_index=False)
    out = pa.BufferOutputStream()
    error_text = 'Artificial Error'

    try:
        with pq.ParquetWriter(out,
                              arrow_table.schema,
                              version='2.0') as writer:

            frames = []
            for i in range(10):
                df['unique_id'] = i
                arrow_table = pa.Table.from_pandas(df, preserve_index=False)
                writer.write_table(arrow_table)
                frames.append(df.copy())
                if i == 5:
                    raise ValueError(error_text)
    except Exception as e:
        assert str(e) == error_text

    buf = out.getvalue()
    result = _read_table(pa.BufferReader(buf))

    expected = pd.concat(frames, ignore_index=True)
    tm.assert_frame_equal(result.to_pandas(), expected)


@pytest.mark.pandas
def test_zlib_compression_bug():
    # ARROW-3514: "zlib deflate failed, output buffer too small"
    table = pa.Table.from_arrays([pa.array(['abc', 'def'])], ['some_col'])
    f = io.BytesIO()
    pq.write_table(table, f, compression='gzip')

    f.seek(0)
    roundtrip = pq.read_table(f)
    tm.assert_frame_equal(roundtrip.to_pandas(), table.to_pandas())


@pytest.mark.pandas
def test_merging_parquet_tables_with_different_pandas_metadata(tempdir):
    # ARROW-3728: Merging Parquet Files - Pandas Meta in Schema Mismatch
    schema = pa.schema([
        pa.field('int', pa.int16()),
        pa.field('float', pa.float32()),
        pa.field('string', pa.string())
    ])
    df1 = pd.DataFrame({
        'int': np.arange(3, dtype=np.uint8),
        'float': np.arange(3, dtype=np.float32),
        'string': ['ABBA', 'EDDA', 'ACDC']
    })
    df2 = pd.DataFrame({
        'int': [4, 5],
        'float': [1.1, None],
        'string': [None, None]
    })
    table1 = pa.Table.from_pandas(df1, schema=schema, preserve_index=False)
    table2 = pa.Table.from_pandas(df2, schema=schema, preserve_index=False)

    assert not table1.schema.equals(table2.schema)
    assert table1.schema.equals(table2.schema, check_metadata=False)

    writer = pq.ParquetWriter(tempdir / 'merged.parquet', schema=schema)
    writer.write_table(table1)
    writer.write_table(table2)


def test_empty_row_groups(tempdir):
    # ARROW-3020
    table = pa.Table.from_arrays([pa.array([], type='int32')], ['f0'])

    path = tempdir / 'empty_row_groups.parquet'

    num_groups = 3
    with pq.ParquetWriter(path, table.schema) as writer:
        for i in range(num_groups):
            writer.write_table(table)

    reader = pq.ParquetFile(path)
    assert reader.metadata.num_row_groups == num_groups

    for i in range(num_groups):
        assert reader.read_row_group(i).equals(table)


@pytest.mark.pandas
def test_parquet_writer_with_caller_provided_filesystem():
    out = pa.BufferOutputStream()

    class CustomFS(FileSystem):
        def __init__(self):
            self.path = None
            self.mode = None

        def open(self, path, mode='rb'):
            self.path = path
            self.mode = mode
            return out

    fs = CustomFS()
    fname = 'expected_fname.parquet'
    df = _test_dataframe(100)
    table = pa.Table.from_pandas(df, preserve_index=False)

    with pq.ParquetWriter(fname, table.schema, filesystem=fs, version='2.0') \
            as writer:
        writer.write_table(table)

    assert fs.path == fname
    assert fs.mode == 'wb'
    assert out.closed

    buf = out.getvalue()
    table_read = _read_table(pa.BufferReader(buf))
    df_read = table_read.to_pandas()
    tm.assert_frame_equal(df_read, df)

    # Should raise ValueError when filesystem is passed with file-like object
    with pytest.raises(ValueError) as err_info:
        pq.ParquetWriter(pa.BufferOutputStream(), table.schema, filesystem=fs)
        expected_msg = ("filesystem passed but where is file-like, so"
                        " there is nothing to open with filesystem.")
        assert str(err_info) == expected_msg


def test_writing_empty_lists():
    # ARROW-2591: [Python] Segmentation fault issue in pq.write_table
    arr1 = pa.array([[], []], pa.list_(pa.int32()))
    table = pa.Table.from_arrays([arr1], ['list(int32)'])
    _check_roundtrip(table)


def test_write_nested_zero_length_array_chunk_failure():
    # Bug report in ARROW-3792
    cols = OrderedDict(
        int32=pa.int32(),
        list_string=pa.list_(pa.string())
    )
    data = [[], [OrderedDict(int32=1, list_string=('G',)), ]]

    # This produces a table with a column like
    # <Column name='list_string' type=ListType(list<item: string>)>
    # [
    #   [],
    #   [
    #     [
    #       "G"
    #     ]
    #   ]
    # ]
    #
    # Each column is a ChunkedArray with 2 elements
    my_arrays = [pa.array(batch, type=pa.struct(cols)).flatten()
                 for batch in data]
    my_batches = [pa.RecordBatch.from_arrays(batch, pa.schema(cols))
                  for batch in my_arrays]
    tbl = pa.Table.from_batches(my_batches, pa.schema(cols))
    _check_roundtrip(tbl)


@pytest.mark.pandas
def test_partitioned_dataset(tempdir):
    # ARROW-3208: Segmentation fault when reading a Parquet partitioned dataset
    # to a Parquet file
    path = tempdir / "ARROW-3208"
    df = pd.DataFrame({
        'one': [-1, 10, 2.5, 100, 1000, 1, 29.2],
        'two': [-1, 10, 2, 100, 1000, 1, 11],
        'three': [0, 0, 0, 0, 0, 0, 0]
    })
    table = pa.Table.from_pandas(df)
    pq.write_to_dataset(table, root_path=str(path),
                        partition_cols=['one', 'two'])
    table = pq.ParquetDataset(path).read()
    pq.write_table(table, path / "output.parquet")


def test_read_column_invalid_index():
    table = pa.table([pa.array([4, 5]), pa.array(["foo", "bar"])],
                     names=['ints', 'strs'])
    bio = pa.BufferOutputStream()
    pq.write_table(table, bio)
    f = pq.ParquetFile(bio.getvalue())
    assert f.reader.read_column(0).to_pylist() == [4, 5]
    assert f.reader.read_column(1).to_pylist() == ["foo", "bar"]
    for index in (-1, 2):
        with pytest.raises((ValueError, IndexError)):
            f.reader.read_column(index)


@pytest.mark.pandas
def test_direct_read_dictionary():
    # ARROW-3325
    repeats = 10
    nunique = 5

    data = [
        [tm.rands(10) for i in range(nunique)] * repeats,

    ]
    table = pa.table(data, names=['f0'])

    bio = pa.BufferOutputStream()
    pq.write_table(table, bio)
    contents = bio.getvalue()

    result = pq.read_table(pa.BufferReader(contents),
                           read_dictionary=['f0'])

    # Compute dictionary-encoded subfield
    expected = pa.table([table[0].dictionary_encode()], names=['f0'])
    assert result.equals(expected)


@pytest.mark.pandas
def test_dataset_read_dictionary(tempdir):
    path = tempdir / "ARROW-3325-dataset"
    t1 = pa.table([[tm.rands(10) for i in range(5)] * 10], names=['f0'])
    t2 = pa.table([[tm.rands(10) for i in range(5)] * 10], names=['f0'])
    pq.write_to_dataset(t1, root_path=str(path))
    pq.write_to_dataset(t2, root_path=str(path))

    result = pq.ParquetDataset(path, read_dictionary=['f0']).read()

    # The order of the chunks is non-deterministic
    ex_chunks = [t1[0].chunk(0).dictionary_encode(),
                 t2[0].chunk(0).dictionary_encode()]

    assert result[0].num_chunks == 2
    c0, c1 = result[0].chunk(0), result[0].chunk(1)
    if c0.equals(ex_chunks[0]):
        assert c1.equals(ex_chunks[1])
    else:
        assert c0.equals(ex_chunks[1])
        assert c1.equals(ex_chunks[0])


@pytest.mark.pandas
def test_direct_read_dictionary_subfield():
    repeats = 10
    nunique = 5

    data = [
        [[tm.rands(10)] for i in range(nunique)] * repeats,
    ]
    table = pa.table(data, names=['f0'])

    bio = pa.BufferOutputStream()
    pq.write_table(table, bio)
    contents = bio.getvalue()
    result = pq.read_table(pa.BufferReader(contents),
                           read_dictionary=['f0.list.item'])

    arr = pa.array(data[0])
    values_as_dict = arr.values.dictionary_encode()

    inner_indices = values_as_dict.indices.cast('int32')
    new_values = pa.DictionaryArray.from_arrays(inner_indices,
                                                values_as_dict.dictionary)

    offsets = pa.array(range(51), type='int32')
    expected_arr = pa.ListArray.from_arrays(offsets, new_values)
    expected = pa.table([expected_arr], names=['f0'])

    assert result.equals(expected)
    assert result[0].num_chunks == 1


@pytest.mark.pandas
def test_dataset_metadata(tempdir):
    path = tempdir / "ARROW-1983-dataset"

    # create and write a test dataset
    df = pd.DataFrame({
        'one': [1, 2, 3],
        'two': [-1, -2, -3],
        'three': [[1, 2], [2, 3], [3, 4]],
    })
    table = pa.Table.from_pandas(df)

    metadata_list = []
    pq.write_to_dataset(table, root_path=str(path),
                        partition_cols=['one', 'two'],
                        metadata_collector=metadata_list)

    # open the dataset and collect metadata from pieces:
    dataset = pq.ParquetDataset(path)
    metadata_list2 = [p.get_metadata() for p in dataset.pieces]

    # compare metadata list content:
    assert len(metadata_list) == len(metadata_list2)
    for md, md2 in zip(metadata_list, metadata_list2):
        d = md.to_dict()
        d2 = md2.to_dict()
        # serialized_size is initialized in the reader:
        assert d.pop('serialized_size') == 0
        assert d2.pop('serialized_size') > 0
        assert d == d2


def test_parquet_file_too_small(tempdir):
    path = str(tempdir / "test.parquet")
    with pytest.raises(pa.ArrowIOError,
                       match='size is 0 bytes'):
        with open(path, 'wb') as f:
            pass
        pq.read_table(path)

    with pytest.raises(pa.ArrowIOError,
                       match='size is 4 bytes'):
        with open(path, 'wb') as f:
            f.write(b'ffff')
        pq.read_table(path)


@pytest.mark.pandas
def test_categorical_index_survives_roundtrip():
    # ARROW-3652, addressed by ARROW-3246
    df = pd.DataFrame([['a', 'b'], ['c', 'd']], columns=['c1', 'c2'])
    df['c1'] = df['c1'].astype('category')
    df = df.set_index(['c1'])

    table = pa.Table.from_pandas(df)
    bos = pa.BufferOutputStream()
    pq.write_table(table, bos)
    ref_df = pq.read_pandas(bos.getvalue()).to_pandas()
    assert isinstance(ref_df.index, pd.CategoricalIndex)
    assert ref_df.index.equals(df.index)


@pytest.mark.pandas
def test_categorical_order_survives_roundtrip():
    # ARROW-6302
    df = pd.DataFrame({"a": pd.Categorical(
        ["a", "b", "c", "a"], categories=["b", "c", "d"], ordered=True)})

    table = pa.Table.from_pandas(df)
    bos = pa.BufferOutputStream()
    pq.write_table(table, bos)

    contents = bos.getvalue()
    result = pq.read_pandas(contents).to_pandas()

    tm.assert_frame_equal(result, df)


def test_dictionary_array_automatically_read():
    # ARROW-3246

    # Make a large dictionary, a little over 4MB of data
    dict_length = 4000
    dict_values = pa.array([('x' * 1000 + '_{}'.format(i))
                            for i in range(dict_length)])

    num_chunks = 10
    chunk_size = 100
    chunks = []
    for i in range(num_chunks):
        indices = np.random.randint(0, dict_length,
                                    size=chunk_size).astype(np.int32)
        chunks.append(pa.DictionaryArray.from_arrays(pa.array(indices),
                                                     dict_values))

    table = pa.table([pa.chunked_array(chunks)], names=['f0'])

    bio = pa.BufferOutputStream()
    pq.write_table(table, bio)
    contents = bio.getvalue()
    result = pq.read_table(pa.BufferReader(contents))

    assert result.equals(table)

    # The only key in the metadata was the Arrow schema key
    assert result.schema.metadata is None


@pytest.mark.pandas
def test_pandas_categorical_na_type_row_groups():
    # ARROW-5085
    df = pd.DataFrame({"col": [None] * 100, "int": [1.0] * 100})
    df_category = df.astype({"col": "category", "int": "category"})
    table = pa.Table.from_pandas(df)
    table_cat = pa.Table.from_pandas(df_category)
    buf = pa.BufferOutputStream()

    # it works
    pq.write_table(table_cat, buf, version="2.0", chunk_size=10)
    result = pq.read_table(buf.getvalue())

    # Result is non-categorical
    assert result[0].equals(table[0])
    assert result[1].equals(table[1])


@pytest.mark.pandas
def test_pandas_categorical_roundtrip():
    # ARROW-5480, this was enabled by ARROW-3246

    # Have one of the categories unobserved and include a null (-1)
    codes = np.array([2, 0, 0, 2, 0, -1, 2], dtype='int32')
    categories = ['foo', 'bar', 'baz']
    df = pd.DataFrame({'x': pd.Categorical.from_codes(
        codes, categories=categories)})

    buf = pa.BufferOutputStream()
    pq.write_table(pa.table(df), buf)

    result = pq.read_table(buf.getvalue()).to_pandas()
    assert result.x.dtype == 'category'
    assert (result.x.cat.categories == categories).all()
    tm.assert_frame_equal(result, df)


@pytest.mark.pandas
def test_multi_dataset_metadata(tempdir):
    filenames = ["ARROW-1983-dataset.0", "ARROW-1983-dataset.1"]
    metapath = str(tempdir / "_metadata")

    # create a test dataset
    df = pd.DataFrame({
        'one': [1, 2, 3],
        'two': [-1, -2, -3],
        'three': [[1, 2], [2, 3], [3, 4]],
        })
    table = pa.Table.from_pandas(df)

    # write dataset twice and collect/merge metadata
    _meta = None
    for filename in filenames:
        meta = []
        pq.write_table(table, str(tempdir / filename),
                       metadata_collector=meta)
        meta[0].set_file_path(filename)
        if _meta is None:
            _meta = meta[0]
        else:
            _meta.append_row_groups(meta[0])

    # Write merged metadata-only file
    with open(metapath, "wb") as f:
        _meta.write_metadata_file(f)

    # Read back the metadata
    meta = pq.read_metadata(metapath)
    md = meta.to_dict()
    _md = _meta.to_dict()
    for key in _md:
        if key != 'serialized_size':
            assert _md[key] == md[key]
    assert _md['num_columns'] == 3
    assert _md['num_rows'] == 6
    assert _md['num_row_groups'] == 2
    assert _md['serialized_size'] == 0
    assert md['serialized_size'] > 0


@pytest.mark.pandas
def test_filter_before_validate_schema(tempdir):
    # ARROW-4076 apply filter before schema validation
    # to avoid checking unneeded schemas

    # create partitioned dataset with mismatching schemas which would
    # otherwise raise if first validation all schemas
    dir1 = tempdir / 'A=0'
    dir1.mkdir()
    table1 = pa.Table.from_pandas(pd.DataFrame({'B': [1, 2, 3]}))
    pq.write_table(table1, dir1 / 'data.parquet')

    dir2 = tempdir / 'A=1'
    dir2.mkdir()
    table2 = pa.Table.from_pandas(pd.DataFrame({'B': ['a', 'b', 'c']}))
    pq.write_table(table2, dir2 / 'data.parquet')

    # read single file using filter
    table = pq.read_table(tempdir, filters=[[('A', '==', 0)]])
    assert table.column('B').equals(pa.chunked_array([[1, 2, 3]]))
