"""
Filters for the Humanitarian Exchange Language (HXL) v1.0
David Megginson
Started October 2014

License: Public Domain
Documentation: https://github.com/HXLStandard/libhxl-python/wiki
"""

import sys, re, six
from copy import copy
import dateutil.parser

import hxl
from hxl.common import pattern_list
from hxl.model import TagPattern, Dataset, Column, Row


#
# Filter-specific exception
#
class HXLFilterException(hxl.HXLException):
    pass

#
# Filter classes
#

class AddFilter(Dataset):
    """
    Composable filter class to add constant values to every row of a HXL dataset.

    This is the class supporting the hxladd command-line utility.

    Because this class is a {@link hxl.model.Dataset}, you can use
    it as the source to an instance of another filter class to build a
    dynamic, single-threaded processing pipeline.
    """

    def __init__(self, source, values, before=False):
        """
        @param source a HXL data source
        @param values a sequence of pairs of Column objects and constant values
        @param before True to add new columns before existing ones
        """
        self.source = source
        if isinstance(values, six.string_types):
            values = [values]
        self.values = [AddFilter.parse_value(value) for value in values]
        self.before = before
        self._columns_out = None

    @property
    def columns(self):
        """
        Add the constant columns to the end.
        """
        if self._columns_out is None:
            new_columns = [value[0] for value in self.values]
            if self.before:
                self._columns_out = new_columns + self.source.columns
            else:
                self._columns_out = self.source.columns + new_columns
            # constant values to add
            self._const_values = [value[1] for value in self.values]
        return self._columns_out

    def __iter__(self):
        return AddFilter.Iterator(self)

    class Iterator:

        def __init__(self, outer):
            self.outer = outer
            self.iterator = iter(outer.source)

        def __next__(self):
            """
            Return the next row, with constant values added.
            """
            row = copy(next(self.iterator))
            row.columns = self.outer.columns
            if self.outer.before:
                row.values = self.outer._const_values + row.values
            else:
                row.values = row.values + self.outer._const_values
            return row

        next = __next__

    VALUE_PATTERN = r'^\s*(?:([^#]*)#)?({token})=(.*)\s*$'.format(token=hxl.common.TOKEN)

    @staticmethod
    def parse_value(s):
        if not isinstance(s, six.string_types):
            return s
        result = re.match(AddFilter.VALUE_PATTERN, s)
        if result:
            header = result.group(1)
            tag = '#' + result.group(2)
            value = result.group(3)
            return (Column(tag=tag, header=header), value)
        else:
            raise HXLFilterException("Badly formatted --value: " + s)


class CacheFilter(Dataset):
    """Composable filter class to cache HXL data in memory."""

    def __init__(self, source, max_rows=None):
        """
        Constructor
        @param max_rows If >0, maximum number of rows to cache.
        """
        self.source = source
        self.max_rows = max_rows
        self.cached_columns = copy(source.columns)
        self.cached_rows = [copy(row) for row in source]
        self.overflow = False

    @property
    def columns(self):
        return self.cached_columns

    def __iter__(self):
        return iter(self.cached_rows)

    def _load(self):
        if self.cached_rows is None:
            self.cached_rows = []
            self.cached_columns = copy(self.source.columns)
            row_count = 0
            for row in self.source:
                row_count += 1
                if self.max_rows > 1 and row_count >= self.max_rows:
                    self.overflow = True
                    break
                else:
                    self.cached_rows.append(copy(row))


class CleanFilter(Dataset):
    """
    Filter for cleaning values in HXL data.
    Can normalise whitespace, convert to upper/lowercase, and fix dates and numbers.
    TODO: clean up lat/lon coordinates
    """

    def __init__(self, source, whitespace=False, upper=[], lower=[], date=[], number=[]):
        """
        Construct a new data-cleaning filter.
        @param source the HXLDataSource
        @param whitespace list of TagPatterns for normalising whitespace, or True to normalise all.
        @param upper list of TagPatterns for converting to uppercase, or True to convert all.
        @param lower list of TagPatterns for converting to lowercase, or True to convert all.
        @param lower list of TagPatterns for normalising dates, or True to normalise all ending in "_date"
        @param lower list of TagPatterns for normalising numbers, or True to normalise all ending in "_num"
        """
        self.source = source
        self.whitespace = whitespace
        self.upper = upper
        self.lower = lower
        self.date = date
        self.number = number

    @property
    def columns(self):
        """Pass on the source columns unmodified."""
        return self.source.columns

    def __iter__(self):
        return CleanFilter.Iterator(self)

    class Iterator:

        def __init__(self, outer):
            self.outer = outer
            self.iterator = iter(outer.source)

        def __next__(self):
            """Return the next row, with values cleaned as needed."""
            # TODO implement a lazy copy
            row = copy(next(self.iterator))
            for i in range(min(len(row.values), len(row.columns))):
                row.values[i] = self._clean_value(row.values[i], row.columns[i])
            return row

        next = __next__

        def _clean_value(self, value, column):
            """Clean a single HXL value."""

            # TODO prescan columns at start for matches

            # Whitespace (-w or -W)
            if self._match_patterns(self.outer.whitespace, column):
                value = re.sub('^\s+', '', value)
                value = re.sub('\s+$', '', value)
                value = re.sub('\s+', ' ', value)

            # Uppercase (-u)
            if self._match_patterns(self.outer.upper, column):
                if sys.version_info[0] > 2:
                    value = value.upper()
                else:
                    value = value.decode('utf8').upper().encode('utf8')

            # Lowercase (-l)
            if self._match_patterns(self.outer.lower, column):
                if sys.version_info[0] > 2:
                    value = value.lower()
                else:
                    value = value.decode('utf8').lower().encode('utf8')

            # Date (-d or -D)
            if self._match_patterns(self.outer.date, column, '_date'):
                if value:
                    value = dateutil.parser.parse(value).strftime('%Y-%m-%d')

            # Number (-n or -N)
            if self._match_patterns(self.outer.number, column, '_num') and re.match('\d', value):
                if value:
                    value = re.sub('[^\d.]', '', value)
                    value = re.sub('^0+', '', value)
                    value = re.sub('(\..*)0+$', '\g<1>', value)
                    value = re.sub('\.$', '', value)

            return value

        def _match_patterns(self, patterns, column, extension=None):
            """Test if a column matches a list of patterns."""
            if not patterns:
                return False
            elif patterns is True:
                # if there's an extension specific like "_date", must match it
                return (column.tag and (not extension or column.tag.endswith(extension)))
            else:
                for pattern in patterns:
                    if pattern.match(column):
                        return True
                return False



class CountFilter(Dataset):
    """
    Composable filter class to aggregate rows in a HXL dataset.

    This is the class supporting the hxlcount command-line utility.

    Because this class is a {@link hxl.model.Dataset}, you can use
    it as the source to an instance of another filter class to build a
    dynamic, single-threaded processing pipeline.

    WARNING: this filter reads the entire source dataset before
    producing output, and may need to hold a large amount of data in
    memory, depending on the number of unique combinations counted.

    Usage:

    <pre>
    filter = CountFilter(source, patterns=['#org', '#sector'])
    </pre>

    or

    <pre>
    filter = source.count(['#org', '#sector'])
    </pre>
    """

    def __init__(self, source, patterns, aggregate_pattern=None):
        """
        Constructor
        @param source the HXL data source
        @param patterns a list of strings or TagPattern objects that form a unique key together
        @param aggregate_pattern an optional tag pattern calculating numeric aggregate values.
        """
        self.source = source
        self.patterns = pattern_list(patterns)
        self.aggregate_pattern = TagPattern.parse(aggregate_pattern) if aggregate_pattern else None
        self._saved_columns = None

    @property
    def columns(self):
        """
        @return the column definitions used in the aggregation report
        """
        if self._saved_columns is None:
            cols = []
            for pattern in self.patterns:
                column = pattern.find_column(self.source.columns)
                if column is not None:
                    header = column.header
                else:
                    header = None
                cols.append(Column(tag=pattern.tag, attributes=pattern.include_attributes, header=header))
            cols.append(Column(tag='#x_count_num', header='Count'))
            if self.aggregate_pattern is not None:
                cols.append(Column(tag='#x_sum_num', header='Sum'))
                cols.append(Column(tag='#x_average_num', header='Average (mean)'))
                cols.append(Column(tag='#x_min_num', header='Minimum value'))
                cols.append(Column(tag='#x_max_num', header='Maximum value'))
            self._saved_columns = cols
        return self._saved_columns

    def __iter__(self):
        return CountFilter.Iterator(self)

    class Iterator:

        def __init__(self, outer):
            self.outer = outer
            self.iterator = iter(outer.source)
            self.aggregate_iter = None

        def __next__(self):
            """
            @return the next row of aggregated data.
            """
            if self.aggregate_iter is None:
                self._aggregate()
            # Write the stats, sorted in value order
            aggregate = next(self.aggregate_iter)
            values = list(aggregate[0])
            values.append(aggregate[1].count)
            if self.outer.aggregate_pattern:
                if aggregate[1].seen_numbers:
                    values.append(aggregate[1].sum)
                    values.append(aggregate[1].average)
                    values.append(aggregate[1].min)
                    values.append(aggregate[1].max)
                else:
                    values = values + ([''] * 4)

            row = Row(self.outer.columns)
            row.values = values
            return row

        next = __next__

        def _aggregate(self):
            """
            Read the entire source dataset and produce saved aggregate data.
            """
            aggregators = {}
            for row in self.iterator:
                values = [pattern.get_value(row) for pattern in self.outer.patterns]
                if values:
                    key = tuple(values)
                    if not key in aggregators:
                        aggregators[key] = Aggregator(self.outer.aggregate_pattern)
                    aggregators[key].add(row)
            self.aggregate_iter = iter(sorted(aggregators.items()))

class Aggregator(object):
    """
    Class to collect aggregates for a single combination.

    Currently calculates count, sum, average, min, and max
    """

    def __init__(self, pattern):
        """
        Constructor
        @param pattern the HXL tag being counted in the row.
        """
        self.pattern = pattern
        self.count = 0
        self.sum = 0.0
        self.average = 0.0
        self.min = None
        self.max = None
        self.seen_numbers = False

    def add(self, row):
        """
        Add a new row of data to the aggregator.
        """
        self.count += 1
        if self.pattern:
            value = self.pattern.get_value(row)
            if value:
                try:
                    n = float(value)
                    self.sum += n
                    self.average = self.sum / self.count
                    if self.min is None or n < self.min:
                        self.min = n
                    if self.max is None or n > self.max:
                        self.max = n
                    self.seen_numbers = True
                except:
                    pass
