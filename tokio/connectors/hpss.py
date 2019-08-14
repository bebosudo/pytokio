"""Connect to various outputs made available by HPSS
"""

import re
import copy
import time
import datetime
from tokio.connectors.common import SubprocessOutputDict

REX_HEADING_LINE = re.compile(r"^[= ]+$")
REX_EMPTY_LINE = re.compile(r"^\s*$")
REX_TIMEDELTA = re.compile(r"^(\d+)-(\d+):(\d+):(\d+)$")

FLOAT_KEYS = set([
    'io_gb',
    'write_gb',
    'read_gb',
    'copy_gb',
    'mig (gb)',
    'purge(gb)',
    'lock%'
])
INT_KEYS = set([
    'users',
    'ops',
    'w_ops',
    'r_ops',
    'c_ops',
    'migfiles',
    'purfiles',
    'count',
    'cleans',
    'locks',
    'mounts',
])
DELTIM_KEYS = set([
    'migtime',
    'purgetime',
    'availtime',
    'locktime',
    'mounttime',
])

REKEY_TABLES = {
    'io totals by client application': 'client',
    'io totals by client host': 'host',
    'io totals by hpss client gateway (ui) host': 'host',
    # 'largest users': 'user', # degenerate users will occur if one user uses multiple client apps
    'migration purge report': 'sc',
    # 'tape drive report': 'drivetyp',
}

class FtpLog(SubprocessOutputDict):
    """Provides an interface for log files containing HPSS FTP transactions

    This connector parses FTP logs generated by HPSS 7.3.  Older versions are
    not supported.

    HPSS FTP log files contain transfer records that look something like::

        Mon Dec 31 00:06:43 2018 0.070 shire-g0.nersc.gov 2581 /home/b/backup/sysinfo/shire-g0/18/12/sum31 b i STOR_Cmd r ftp backup
        Mon Dec 31 00:06:43 2018 0.020 shire-g0.nersc.gov 0 /home/b/backup/sysinfo/shire-g0/18/12/hsi_sudo.log31 b i STOR_Cmd r ftp backup
        Mon Dec 31 00:06:44 2018 0.470 sgn-pub-01.nersc.gov 0 /home/r/regan/www/HipMer/s_1_2_sequence.fastq.gz b o RETR_Cmd r ftp wwwhpss
        Mon Dec 31 00:06:46 2018 dtn01-int.nersc.gov /home/o/operator/.check_ftp.25651 b POPN_Cmd w r ftp operator fd 0
        Mon Dec 31 00:06:46 2018 0.080 dtn01-int.nersc.gov 33 /home/o/operator/.check_ftp.25651 b i PSTO_Cmd r ftp operator fd 0
        Mon Dec 31 00:06:46 2018 dtn01-int.nersc.gov /home/o/operator/.check_ftp.25651 b POPN_Cmd r r ftp operator fd 0

    """
#0   1   2  3        4    5                   6                                  7          8                 9 10 11       12       13  14       15 16
#Mon Dec 31 00:06:46 2018 dtn01-int.nersc.gov /home/o/operator/.check_ftp.25651  b          POPN_Cmd          r r  ftp      operator fd  0
#Mon Dec 31 00:06:46 2018 0.010               dtn01-int.nersc.gov                33         /home/o/opera...  b o  PRTR_Cmd r        ftp operator fd 0
#Mon Dec 31 00:06:48 2018 0.430               sgn-pub-01.nersc.gov               0          /home/r/regan...  b o  RETR_Cmd r        ftp wwwhpss
#Mon Feb  4 16:45:04 2019 457.800             sgn-pub-01.nersc.gov               7184842752 /home/r/regan...  b o  RETR_Cmd r        ftp wwwhpss
#Fri Jul 12 15:32:43 2019 2.080               gert01-224.nersc.gov               2147483647 /home/n/nickb...  b i  PSTO_Cmd r        ftp nickb    fd 0
#Mon Jul 29 15:44:22 2019 0.800               dtn02.nersc.gov                    464566784  /home/n/nickb...  b o  PRTR_Cmd r        ftp nickb    fd 0

    def __init__(self, *args, **kwargs):
        super(FtpLog, self).__init__(*args, **kwargs)
        self.load()

    @classmethod
    def from_str(cls, input_str):
        """Instantiate from a string
        """
        return cls(from_string=input_str)

    @classmethod
    def from_file(cls, cache_file):
        """Instantiate from a cache file
        """
        return cls(cache_file=cache_file)

    def load_str(self, input_str):
        """Parse text from an HPSS FTP log
        """
        skip_recs = 0
        for line in input_str.splitlines():
            args = line.split()
            rec = {
                'end_timestamp': time.mktime(datetime.datetime.strptime(" ".join(args[0:5]), "%a %b %d %H:%M:%S %Y").timetuple()),
            }
            # HPSS 7.4 POPN command can be skipped
            if args[11] == "ftp" and args[8] == "POPN_Cmd":
                skip_recs += 1
                continue

            # this is the signature of HPSS 7.3 log files
            app = None
            if args[13] == "ftp" and args[11].endswith('_Cmd'):
                # STOR = serial ftp; PTSO = pftp; LFSTO = "local file command"
                # determine directionality
                if args[11] in {"STOR_Cmd", "PSTO_Cmd", "LFSTO_Cmd"}:
                    rec['opname'] = "LH" # putting data into HPSS
                elif args[11] in {"RETR_Cmd", "PRTR_Cmd", "LFRTR_Cmd"}:
                    rec['opname'] = "HL" # getting data from HPSS

                # determine protocol
                if args[11].startswith("P"):
                    app = "pftp"
                elif args[11].startswith('LF'):
                    # wtf even are these
                    app = "pftp"
                else:
                    app = "ftp"

            if not app:
                skip_recs += 1
                continue

            rec['duration_sec'] = float(args[5])
            rec['remote_host'] = args[6]
            rec['bytes'] = int(args[7])
            # dest_path is unknown
            # access_latency is unknown

            # this is kind of messy; end_timestamp is always expressed at
            # one-second resolution, while duration_sec is resolved down to
            # milliseconds.  Thus the start_timestamp can misrepresent the
            # actual millisecond start time which will misrepresent very fast
            # transfers.
            rec['start_timestamp'] = rec['end_timestamp'] - rec['duration_sec']

            # sometimes duration_sec is 0.000 because the logfile contains very limited precision
            if rec['duration_sec'] > 0.0:
                rec['bytes_sec'] = rec['bytes'] / rec['duration_sec'] 

            if app not in self:
                self[app] = []
            self[app].append(rec)


class HsiLog(SubprocessOutputDict):
    """Provides an interface for log files containing HSI and HTAR transactions

    Results in a dictionary-like object of the following format::

        {
            "hsi": [
                {
                    "access_latency_sec": 0.03,
                    "account_id": 35136,
                    "bytes": 0,
                    "bytes_sec": 0.0,
                    "client_pid": 1035,
                    "cos_id": 0,
                    "dest_path": "/home/g/glock/blah.bin",
                    "hpss_uid": 35136,
                    "opname": "LH",
                    "remote_host": "someuniv.edu",
                    "return_code": -1,
                    "source_path": "blah.bin",
                    "end_timestamp": 1565420701
                },
                ...
            "htar": [
                {
                    "account_id": 58888,
                    "bytes": 58178668032,
                    "bytes_sec": 146472.0,
                    "client_pid": 14301,
                    "cos_id": 5,
                    "duration_sec": 397.2,
                    "hpss_path": "/nersc/projects/blah.tar",
                    "hpss_uid": 58888,
                    "htar_op": "create",
                    "opname": "LH",
                    "remote_ftp_host": "",
                    "remote_host": "cori02-224.nersc.gov",
                    "return_code": 0,
                    "end_timestamp": 1565420728
                }
            ]
        }

    where the top-level keys are either "hsi" or "htar", and their values are
    lists containing every HSI or HTAR transaction, respectively.

    The keys generally follow the raw nomenclature used in the HSI logs which
    can be found on `Mike Gleicher's website`_.  Perhaps most relevant are the
    opnames, which can be one of

    * FU - file unlink.  Has no destination filename field or account id.
    * FR - file rename.  Has no account id.
    * LH - transfer into HPSS ("Local to HPSS")
    * HL - transfer out of HPSS ("HPSS to Local")
    * HH - internal file copy ("HPSS-to-HPSS")

    For posterity,

    - ``access_latency_sec`` is the time to open the file.  This includes the
      latency to pull the tape and insert it into the drive.
    - ``bytes`` and ``bytes_sec`` are the size and rate of data transfer
    - ``duration_sec`` is the time to complete the transfer
    - ``return_code`` is zero on success, nonzero otherwise

    .. Mike Gleicher's website: http://pal.mgleicher.us/HSI_Admin/log_files.html

    """
    def __init__(self, *args, **kwargs):
        super(HsiLog, self).__init__(*args, **kwargs)
        self.load()

    @classmethod
    def from_str(cls, input_str):
        """Instantiate from a string
        """
        return cls(from_string=input_str)

    @classmethod
    def from_file(cls, cache_file):
        """Instantiate from a cache file
        """
        return cls(cache_file=cache_file)

    def load_str(self, input_str):
        """Parse an HSI log file containing HSI and HTAR transactions
        """
        bad_recs = 0
        for line in input_str.splitlines():
            # log lines use spaces/tabs to mean different things, e.g.,
            # Sat Aug 10 00:05:13 2019 bucket.ssl.berkeley.edu hsi 69615 1360^ILH^I-1^I0.03^I0^I0.0^I0^Is6c0.out.20190727_085506.1^I/home/c/cobb/seti/s6_data/ao/s6c0/2019/s6c0.out.20190727_085506.1^I69615
            args = line.split(' ', 8)
            app = args[6] # hsi or htar
            app_args = args.pop()
            args += app_args.split('\t')

            if app == 'hsi':
                rec = {
                    'client_pid': int(args[8]),
                    'opname': args[9],
                    'return_code': int(args[10]),
                    'access_latency_sec': float(args[11]),
                    'bytes': int(args[12]),
                    'bytes_sec': 1000.0 * float(args[13]),
                    'cos_id': int(args[14]),
                    'source_path': args[15],
                }
                # not all operations (e.g., FU) have the final two fields
                if args[9] != 'FU':
                    rec['dest_path'] = args[16]
                    if args[9] != 'FR':
                        rec['account_id'] = int(args[17])

                # bytes_sec will be zero for failed ops, metadata ops, etc
                if rec['bytes_sec'] > 0.0:
                    rec['duration_sec'] = rec['bytes'] / rec['bytes_sec']
                else:
                    rec['duration_sec'] = 0.0
            elif app == 'htar':
                rec = {
                    'client_pid': int(args[8]),
                    'htar_op': args[9],
                    'opname': args[10],
                    'return_code': int(args[11]),
                    'bytes': int(args[12]),
                    'duration_sec': float(args[13]),
                    'bytes_sec': float(args[14]),
                    'hpss_path': args[15],
                    'cos_id': int(args[-2]),
                    'account_id': int(args[-1])
                }
                if len(args) == 19:
                    # whose idea was it to make the column count variable?
                    rec['remote_ftp_host'] = args[-3]
            else:
                bad_recs += 1
                continue

            rec['hpss_uid'] = int(args[7])
            rec['remote_host'] = args[5]
            rec['end_timestamp'] = time.mktime(datetime.datetime.strptime(" ".join(args[0:5]), "%a %b %d %H:%M:%S %Y").timetuple())
            rec['start_timestamp'] = rec['end_timestamp'] - rec['duration_sec']

            if app not in self:
                self[app] = []
            self[app].append(rec)

class HpssDailyReport(SubprocessOutputDict):
    """Representation for the daily report that HPSS can generate
    """
    def __init__(self, *args, **kwargs):
        super(HpssDailyReport, self).__init__(*args, **kwargs)
        self.date = None
        self.load()

    @classmethod
    def from_str(cls, input_str):
        """Instantiate from a string
        """
        return cls(from_string=input_str)

    @classmethod
    def from_file(cls, cache_file):
        """Instantiate from a cache file
        """
        return cls(cache_file=cache_file)

    def load_str(self, input_str):
        """Parse the HPSS daily report text
        """
        lines = input_str.splitlines()
        num_lines = len(lines)
        start_line = 0

        # Look for the header for the whole report to get the report date
        for start_line, line in enumerate(lines):
            if line.startswith("HPSS Report for Date"):
                self.date = datetime.datetime.strptime(line.split()[-1], "%Y-%m-%d")
                break
        if not self.date:
            raise IndexError("No report date found")

        # Try to find tables encoded in the remainder of the report
        while start_line < num_lines:
            parsed_table, finish_line = _parse_section(lines, start_line)
            if finish_line != start_line and 'records' in parsed_table:
                if parsed_table['system'] not in self:
                    self.__setitem__(parsed_table['system'], {})

                # convert a list of records into a dict of indices
                if parsed_table['title'] in REKEY_TABLES:
                    parsed_table = _rekey_table(parsed_table,
                                                key=REKEY_TABLES[parsed_table['title']])

                self[parsed_table['system']][parsed_table['title']] = parsed_table['records']
            start_line += 1

def _parse_section(lines, start_line=0):
    """Parse a single table of the HPSS daily report

    Converts a table from the HPSS daily report into a dictionary.  For example
    an example table may appear as::

        Archive : IO Totals by HPSS Client Gateway (UI) Host
        Host             Users      IO_GB       Ops
        ===============  =====  =========  ========
        heart               53   148740.6     27991
        dtn11                5    29538.6      1694
        Total               58   178279.2     29685
        HPSS ACCOUNTING:         224962.6

    which will return a dict of form::

        {
            "system": "archive",
            "title": "io totals by hpss client gateway (ui) host",
            "records": {
                "heart": {
                    "io_gb": "148740.6",
                    "ops": "27991",
                    "users": "53",
                },
                "dtn11": {
                    "io_gb": "29538.6",
                    "ops": "1694",
                    "users": "5",
                },
                "total": {
                    "io_gb": "178279.2",
                    "ops": "29685",
                    "users": "58",
                }
            ]
        }

    This function is robust to invalid data, and any lines that do not appear to
    be a valid table will be treated as the end of the table.

    Args:
        lines (list of str): Text of the HPSS report
        start_line (int): Index of ``lines`` defined such that

          * ``lines[start_line]`` is the table title
          * ``lines[start_line + 1]`` is the table heading row
          * ``lines[start_line + 2]`` is the line separating the table heading and
            the first row of data
          * ``lines[start_line + 3:]`` are the rows of the table

    Returns:
        tuple:
          Tuple of (dict, int) where

          * dict contains the parsed contents of the table
          * int is the index of the last line of the table + 1
    """
    results = {}

    # Skip any initial whitespace
    num_lines = len(lines)
    while start_line < num_lines and REX_EMPTY_LINE.match(lines[start_line]):
        start_line += 1

    # Did we skip past the end of the input data?
    if start_line >= num_lines:
        return results, start_line

    # Parse table title (if available).  This can pick up times (0:00:00) so do
    # not treat system, title as legitimate values until we also identify the
    # line below column headings.
    if ':' not in lines[start_line]:
        return results, start_line
    system, title = lines[start_line].split(':', 1)

    # Determine column delimiters
    separator_line = lines[start_line + 2]
    col_extents = _find_columns(separator_line)
    if len(col_extents) == 0:
        return results, start_line

    # At this point, we are reasonably confident we have found a table.
    # Populate results so that this function returns some indicator of
    # success.
    results['system'] = system.strip().lower()
    results['title'] = title.strip().lower()

    # Determine column headers
    heading_line = lines[start_line + 1]
    headings = []
    for start_pos, str_len in col_extents:
        headings.append(heading_line[start_pos:start_pos + str_len].strip())

    records = []
    index = 0
    for index, line in enumerate(lines[start_line + 3:]):
        # check for end of record (empty line)
        if REX_EMPTY_LINE.match(line):
            # an empty line denotes end of table
            break
        elif len(line) < (col_extents[-1][0] + col_extents[-1][1] - 1):
            # line is malformed; this happens for table summaries
            break

        record = {}
        for heading_idx, (start_pos, str_len) in enumerate(col_extents):
            col_name = headings[heading_idx].lower()
            col_val = line[start_pos:start_pos + str_len].lower().strip()
            if col_name in FLOAT_KEYS:
                record[col_name] = float(col_val)
            elif col_name in INT_KEYS:
                record[col_name] = int(col_val)
            elif col_name in DELTIM_KEYS:
                record[col_name] = col_val
                record[col_name + "secs"] = _hpss_timedelta_to_secs(col_val)
            else:
                record[col_name] = col_val
        records.append(record)

    if records:
        results['records'] = records

    return (results, index + 1)

def _find_columns(line, sep="=", gap=' ', strict=False):
    """Determine the column start/end positions for a header line separator

    Takes a line separator such as the one denoted below:

        Host             Users      IO_GB
        ===============  =====  =========
        heart               53   148740.6

    and returns a tuple of (start index, end index) values that can be used to
    slice table rows into column entries.

    Args:
        line (str): Text comprised of separator characters and spaces that
            define the extents of columns
        sep (str): The character used to draw the column lines
        gap (str): The character separating ``sep`` characters
        strict (bool): If true, restrict column extents to only include sep
            characters and not the spaces that follow them.

    Returns:
        list of tuples:
    """

    columns = []

    # if line is not comprised exclusively of separators and gaps, it is not a
    # valid heading line
    if line.replace(sep, 'X').replace(gap, 'X').strip('X') != "":
        return columns

    if strict:
        col_start = None
    else:
        col_start = 0

    for index, char in enumerate(line):
        if strict:
            # col_start == None == looking for start of a new column
            if col_start is None and char == sep:
                col_start = index
            # if this is the end of an inter-column gap
            elif index > 0 and char == gap and line[index - 1] == sep:
                columns.append((col_start, index - col_start))
                col_start = None
        else:
            # if this is the end of an inter-column gap
            if index > 0 and char == gap and line[index - 1] == sep:
                columns.append((col_start, index - col_start))
                col_start = index

    if line and line[-1] == sep and col_start is not None:
        columns.append((col_start, len(line) - col_start))

    return columns

def _rekey_table(table, key):
    """Converts a list of records into a dict of records

    Converts a table of records as returned by _parse_section() of the form::

        {
            "records": [
                {
                    "host": "heart",
                    "io_gb": "148740.6",
                    "ops": "27991",
                    "users": "53",
                },
                ...
            ]
        }

    Into a table of key-value pairs the form::

        {
            "records": {
                "heart": {
                    "io_gb": "148740.6",
                    "ops": "27991",
                    "users": "53",
                },
                ...
            }
        }

    Does not handle degenerate keys when re-keying, so only some tables with a
    uniquely identifying key can be rekeyed.

    Args:
        table (dict): Output of the _parse_section() function
        key (str): Key to pull out of each element of table['records'] to use as
            the key for each record

    Returns:
        dict: Table with records expressed as key-value pairs instead of a list
    """
    new_table = copy.deepcopy(table)
    new_records = {}

    for record in new_table['records']:
        new_key = record.pop(key)
        if new_key in new_records:
            raise KeyError("Degenerate key %s=%s" % (key, new_key))
        new_records[new_key] = record

    new_table['records'] = new_records

    return new_table

def _hpss_timedelta_to_secs(timedelta_str):
    """Convert HPSS-encoded timedelta string into seconds

    Args:
        timedelta_str (str): String in form d-HH:MM:SS where d is the number of
            days, HH is hours, MM minutes, and SS seconds

    Returns:
        int: number of seconds represented by timedelta_str
    """

    match = REX_TIMEDELTA.match(timedelta_str)
    if match:
        seconds = int(match.group(1)) * 86400
        seconds += int(match.group(2)) * 3600
        seconds += int(match.group(3)) * 60
        seconds += int(match.group(4))
    else:
        seconds = -1

    return seconds
