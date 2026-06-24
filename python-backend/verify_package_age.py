#!/usr/bin/env python3
"""
verify_package_age.py — Blocks CI/CD if python packages are newer than THRESHOLD_DAYS.
Compatible with hash-enforced requirements.txt files.
Usage: python verify_package_age.py
"""

import sys
import json
import time
import datetime
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

THRESHOLD_DAYS = 30
REQUIREMENTS_FILE = 'requirements.txt'
REPORT_FILE = 'package_age_report.json'
MAX_WORKERS = 10
print_lock = Lock()


def get_publish_date_and_hashes(pkg: str, ver: str):
    url = f'https://pypi.org/pypi/{pkg}/json'
    retry_delays = [1, 3, 5, 10, 15]  # Backoff delays in seconds
    attempt = 0

    while attempt <= len(retry_delays):
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.7727.56 Safari/537.36'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())

                releases = data.get('releases', {})
                if ver not in releases or not releases[ver]:
                    print(
                        f'  WARNING: Version {ver} not found in PyPI metadata for {pkg}')
                    return None, set()

                upload_time_str = releases[ver][0]['upload_time_iso_8601']
                if upload_time_str.endswith('Z'):
                    upload_time_str = upload_time_str[:-1] + '+00:00'

                pub_date = datetime.datetime.fromisoformat(upload_time_str)

                # Collect all official hashes from PyPI for this version
                pypi_hashes = set()
                for artifact in releases[ver]:
                    digests = artifact.get('digests', {})
                    for algo, value in digests.items():
                        pypi_hashes.add(f'{algo}:{value}')

                return pub_date, pypi_hashes

        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < len(retry_delays):
                    wait_time = retry_delays[attempt]
                    print(
                        f'  INFO: Rate limited (429) for {pkg}, retrying in {wait_time}s...')
                    time.sleep(wait_time)
                    attempt += 1
                    continue
                else:
                    print(
                        f'  WARNING: Rate limited (429) for {pkg}, exhausted all retries')
                    return None, set()
            elif e.code == 404:
                print(
                    f'  WARNING: Package {pkg} not found on PyPI (Internal or private package?)')
            else:
                print(f'  WARNING: HTTP error fetching {pkg}: {e}')
            return None, set()
        except Exception as e:
            print(f'  WARNING: Cannot fetch {pkg}=={ver}: {e}')
            return None, set()
    return None, set()  # ← ADD THIS — safety net if while loop exits naturally


def parse_requirements(fp: str):
    pkgs = []
    if not Path(fp).exists():
        print(f"Error: Requirements file '{fp}' not found.")
        sys.exit(1)

    current_pkg = None
    current_hashes = []

    for line in Path(fp).read_text().splitlines():
        # Clean whitespaces
        line = line.strip()

        # Handle line continuations — collect the continued line as-is
        has_continuation = line.endswith('\\')
        if has_continuation:
            line = line[:-1].strip()

        # Strip inline comments
        line = line.split('#')[0].strip()

        if not line:
            continue

        # Collect hash lines associated with the current package
        if line.startswith('--hash'):
            # e.g. --hash=sha256:abcdef...
            hash_part = line.split('=', 1)
            if len(hash_part) == 2:
                current_hashes.append(hash_part[1].strip())
            continue

        # If we hit a new non-hash line and have a pending package, flush it
        if current_pkg is not None:
            pkgs.append((current_pkg[0], current_pkg[1], current_hashes))
            current_pkg = None
            current_hashes = []

        # Handle environment markers
        if ';' in line:
            line = line.split(';')[0].strip()

        if '==' in line:
            n, v = line.split('==', 1)
            n = n.strip()
            v = v.strip()

            # Clean up packaging extras (e.g., psycopg[binary] -> psycopg)
            if '[' in n and n.endswith(']'):
                n = n.split('[')[0].strip()

            current_pkg = (n, v)

    # Flush the last package
    if current_pkg is not None:
        pkgs.append((current_pkg[0], current_pkg[1], current_hashes))

    return pkgs


def check_hashes(name: str, ver: str, local_hashes: list, pypi_hashes: set):
    """
    Compare hashes declared in requirements.txt against those published on PyPI.
    Returns (passed: bool, mismatches: list, unrecognised: list)
    """
    if not local_hashes:
        # No hashes declared in requirements.txt — nothing to verify
        return True, [], []

    if not pypi_hashes:
        # PyPI returned no hash data (private/missing package)
        return None, [], []

    mismatches = []
    unrecognised = []

    for h in local_hashes:
        if h in pypi_hashes:
            continue  # This hash exists on PyPI — good
        # Check if PyPI even has ANY hash with the same algorithm
        algo = h.split(':')[0] if ':' in h else ''
        algo_present = any(ph.startswith(f'{algo}:') for ph in pypi_hashes)
        if algo_present:
            mismatches.append(h)   # Same algo, different value — tampered?
        else:
            # Algo not published by PyPI for this release
            unrecognised.append(h)

    passed = len(mismatches) == 0
    return passed, mismatches, unrecognised


def check_package(name: str, ver: str, local_hashes: list, now: datetime.datetime):
    """Check a single package and return result dict."""
    pub, pypi_hashes = get_publish_date_and_hashes(name, ver)

    # --- Age check ---
    if pub is None:
        with print_lock:
            print(f'  ⚠️  UNKNOWN: {name}=={ver} (Skipped safety check)')
        return {'package': f'{name}=={ver}', 'status': 'unknown', 'age': None,
                'hash_status': 'unknown', 'hash_mismatches': []}

    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=datetime.timezone.utc)

    age = (now - pub).days
    age_passed = age >= THRESHOLD_DAYS

    # --- Hash check ---
    hash_ok, mismatches, unrecognised = check_hashes(
        name, ver, local_hashes, pypi_hashes)

    # Determine combined status
    if not age_passed:
        age_label = f'❌ FAIL (age): {name}=={ver} — only {age} day(s) old!'
    else:
        age_label = f'✅ PASS (age): {name}=={ver} — {age} days old'

    if hash_ok is None:
        hash_label = f'  ⚠️  HASH UNKNOWN: could not retrieve PyPI hashes'
        hash_status = 'unknown'
    elif not local_hashes:
        hash_label = f'  ℹ️  HASH SKIP: no hashes declared in requirements.txt'
        hash_status = 'skipped'
    elif hash_ok and not unrecognised:
        hash_label = f'  ✅ HASH PASS: all {len(local_hashes)} hash(es) verified against PyPI'
        hash_status = 'pass'
    elif hash_ok and unrecognised:
        hash_label = (f'  ⚠️  HASH PARTIAL: {len(unrecognised)} hash algo(s) not published by '
                      f'PyPI for this release (unverifiable): {unrecognised}')
        hash_status = 'partial'
    else:
        hash_label = (f'  ❌ HASH FAIL: {len(mismatches)} hash(es) do NOT match PyPI — '
                      f'possible tampering! {mismatches}')
        hash_status = 'fail'

    with print_lock:
        print(f'  {age_label}')
        print(hash_label)

    # Overall status: fail if either age or hash fails
    if not age_passed or hash_status == 'fail':
        status = 'fail'
    elif hash_status == 'unknown':
        status = 'unknown'
    else:
        status = 'pass'

    return {
        'package': f'{name}=={ver}',
        'status': status,
        'age': age,
        'hash_status': hash_status,
        'hash_mismatches': mismatches,
        'hash_unrecognised': unrecognised,
    }


def generate_report(results: list):
    """Generate and save JSON report for artifacts."""
    failures = [r for r in results if r['status'] == 'fail']
    unknowns = [r for r in results if r['status'] == 'unknown']
    passes = [r for r in results if r['status'] == 'pass']

    report = {
        'threshold_days': THRESHOLD_DAYS,
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'summary': {
            'total_packages': len(results),
            'passed': len(passes),
            'failed': len(failures),
            'unknown': len(unknowns)
        },
        'failures': failures,
        'unknowns': unknowns,
        'passes': passes
    }

    with open(REPORT_FILE, 'w') as f:
        json.dump(report, f, indent=2)

    return report


REPORT_HTML_FILE = 'package_age_report.html'


def generate_html_report(report: dict):
    ts = report['timestamp']
    threshold = report['threshold_days']
    summary = report['summary']

    def status_badge(status):
        colors = {
            'fail':    ('#c0392b', '✖ FAIL'),
            'pass':    ('#27ae60', '✔ PASS'),
            'unknown': ('#e67e22', '? UNKNOWN'),
            'partial': ('#f39c12', '~ PARTIAL'),
            'skipped': ('#7f8c8d', '– SKIP'),
        }
        bg, label = colors.get(status, ('#95a5a6', status.upper()))
        return (f'<span style="background:{bg};color:#fff;padding:2px 10px;'
                f'border-radius:4px;font-size:12px;font-weight:700;">{label}</span>')

    def rows_for(items):
        html = ''
        for r in items:
            age_str = f'{r["age"]} days' if r["age"] is not None else '—'
            mismatches = ', '.join(r.get('hash_mismatches', [])) or '—'
            row_bg = '#fff3f3' if r['status'] == 'fail' else '#ffffff'
            html += f'''<tr style="background:{row_bg};border-bottom:1px solid #ecf0f1;">
  <td style="padding:10px 14px;font-family:monospace;font-size:13px;">{r["package"]}</td>
  <td style="padding:10px 14px;text-align:center;">{status_badge(r["status"])}</td>
  <td style="padding:10px 14px;text-align:center;font-size:13px;">{age_str}</td>
  <td style="padding:10px 14px;text-align:center;">{status_badge(r.get("hash_status","skipped"))}</td>
  <td style="padding:10px 14px;font-family:monospace;font-size:11px;color:#c0392b;">{mismatches}</td>
</tr>'''
        return html

    all_results = report['failures'] + report['unknowns'] + report['passes']
    overall_color = '#c0392b' if summary['failed'] > 0 else '#27ae60'
    overall_label = 'FAILED' if summary['failed'] > 0 else 'PASSED'

    html = f'''<h2 style="font-family:sans-serif;">📦 Package Age &amp; Hash Report</h2>
<p style="font-family:sans-serif;font-size:13px;color:#7f8c8d;">Generated: {ts} &nbsp;|&nbsp; Threshold: {threshold} days</p>
<p style="font-family:sans-serif;font-size:18px;font-weight:700;color:#fff;background:{overall_color};padding:14px 20px;border-radius:8px;">Overall Status: {overall_label}</p>
<table style="width:100%;border-collapse:collapse;font-family:sans-serif;margin-bottom:24px;">
  <thead>
    <tr style="background:#2c3e50;color:#fff;">
      <th style="padding:10px 14px;text-align:left;">Package</th>
      <th style="padding:10px 14px;text-align:center;">Status</th>
      <th style="padding:10px 14px;text-align:center;">Age</th>
      <th style="padding:10px 14px;text-align:center;">Hash</th>
      <th style="padding:10px 14px;text-align:left;">Hash Mismatches</th>
    </tr>
  </thead>
  <tbody>
    {rows_for(all_results)}
  </tbody>
</table>'''

    with open(REPORT_HTML_FILE, 'w') as f:
        f.write(html)


def main():
    print(
        f'=== Package Age & Hash Check (Threshold: {THRESHOLD_DAYS} days) ===')

    packages = parse_requirements(REQUIREMENTS_FILE)
    if not packages:
        print("No pinned packages found to validate.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)

    # Use ThreadPoolExecutor for parallel package checking
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_package, name, ver, hashes, now): (name, ver)
                   for name, ver, hashes in packages}

        for future in as_completed(futures):
            result = future.result()
            results.append(result)

    # Generate reports
    report = generate_report(results)
    generate_html_report(report)

    print('\n=== Summary ===')
    print(f'Total packages checked: {report["summary"]["total_packages"]}')
    print(f'Passed: {report["summary"]["passed"]}')
    print(f'Failed: {report["summary"]["failed"]}')
    print(f'Unknown: {report["summary"]["unknown"]}')
    print(f'Reports saved to: {REPORT_FILE}, {REPORT_HTML_FILE}')

    if report['summary']['failed'] > 0:
        print(
            f'\nDetected {report["summary"]["failed"]} package(s) breaking the {THRESHOLD_DAYS}-day age policy or hash verification:')
        for item in report['failures']:
            print(
                f'  - {item["package"]} (age: {item["age"]} days, hash: {item["hash_status"]})')
        sys.exit(1)

    if report['summary']['unknown'] > 0:
        print(
            f'\nPassed, but {report["summary"]["unknown"]} private or missing packages could not be checked.')
    else:
        print(
            '\nAll packages successfully verified against the age policy and hash checks.')


if __name__ == '__main__':
    main()
