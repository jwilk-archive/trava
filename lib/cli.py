# Copyright © 2016-2020 Jakub Wilk <jwilk@jwilk.net>
# SPDX-License-Identifier: MIT

'''
command-line interface
'''

import argparse
import collections
import io
import json
import re
import shutil
import subprocess
import sys
import types
import urllib.parse
import urllib.request

import lib.colors
import lib.pager

user_agent = 'trava (https://github.com/jwilk/trava)'

def get(url, headers=()):
    headers = dict(headers)
    headers.update(
        {'User-Agent': user_agent}
    )
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request)

def get_json(url, headers=()):
    headers = dict(headers)
    headers.update(
        {'Content-Type': 'application/vnd.travis-ci.2+json'}
    )
    with get(url, headers) as fp:
        with io.TextIOWrapper(fp, encoding='UTF-8') as tfp:
            return json.load(tfp)

_dispatch = []

def dispatch(regex):
    def decorator(cmd):
        _dispatch.append((regex, cmd))
        return cmd
    return decorator

def get_git_url():
    try:
        url = subprocess.check_output('git ls-remote --get-url'.split())
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128:
            return
        raise
    url = url.decode('ASCII').rstrip()
    (scheme, netloc, path, query, fragment) = urllib.parse.urlsplit(url)
    if netloc == 'github.com':
        if path.endswith('.git'):
            path = path[:-4]
        return path
    else:
        return

def main():
    ap = argparse.ArgumentParser()
    ag = ap.add_mutually_exclusive_group()
    ag.add_argument('--raw-cr', action='store_true')
    ag.add_argument('--timestamps', action='store_true')
    ap.add_argument('url', metavar='URL')
    options = ap.parse_args()
    if options.url == '.':
        options.url = get_git_url()
    url = urllib.parse.urljoin('https://travis-ci.org/', options.url)
    (scheme, netloc, path, query, fragment) = urllib.parse.urlsplit(url)
    if scheme not in {'http', 'https'}:
        ap.error('unsupported URL')
    if netloc != 'travis-ci.org':
        ap.error('unsupported URL')
    for regex, cmd in _dispatch:
        regex = ('/' if regex else '') + regex
        regex = fr'\A/(?:github/)?(?P<project>[\w.-]+/[\w.-]+){regex}\Z'
        match = re.match(regex, path)
        if match is not None:
            break
    else:
        ap.error('unsupported URL')
    with lib.pager.autopager():
        return cmd(options, **match.groupdict())

@dispatch('')
@dispatch('branches')
def show_branches(options, project):
    url = f'https://api.travis-ci.org/repos/{project}/branches'
    data = get_json(url)
    commits = {c['id']: c for c in data['commits']}
    for branch in data['branches']:
        commit = commits[branch['commit_id']]
        template = '#{number} {branch} {state}'
        curious = False
        if branch['finished_at'] is None:
            template = '{t.yellow}' + template
        elif branch['state'] != 'passed':
            template = '{t.bold}{t.red}' + template
            curious = True
        lib.colors.print(template,
            number=branch['number'],
            branch=commit['branch'],
            state=branch['state'],
        )
        branch_id = branch['id']
        url = f'https://travis-ci.org/{project}/builds/{branch_id}'
        template = '{t.cyan}'
        if curious:
            template += '{t.bold}'
        template += '{url}{t.off}'
        lib.colors.print(template, url=url, space='')
        print()

@dispatch(r'builds/(?P<build_id>\d+)')
def show_build(options, project, build_id):
    url = f'https://api.travis-ci.org/repos/{project}/builds/{build_id}'
    data = get_json(url)
    config_coll = collections.defaultdict(set)
    matrix = data['matrix']
    config_keys = collections.Counter()
    for job in matrix:
        config_keys.update(job['config'].keys())
    def config_sort(item):
        (key, value) = item
        return (-config_keys[key], key)
    for job in matrix:
        for key in config_keys:
            value = job['config'].get(key)
            if isinstance(value, (dict, list)):
                continue
            config_coll[key].add(value)
    for job in matrix:
        template = '#{number} {config}'
        error = False
        if job['finished_at'] is None:
            template = '{t.yellow}' + template
        elif job['result'] != 0:
            error = True
            template = '{t.bold}{t.red}' + template
        template += '{t.off}'
        config = []
        for key, value in sorted(job['config'].items(), key=config_sort):
            if key.startswith('.'):
                continue
            if isinstance(value, (dict, list)):
                continue
            if len(config_coll[key]) == 1:
                continue
            config += [f'{key}={value}']
        config = ' '.join(config)
        lib.colors.print(template, number=job['number'], config=config)
        job_id = job['id']
        url = f'https://travis-ci.org/{project}/jobs/{job_id}'
        template = '{t.cyan}'
        if error:
            template += '{t.bold}'
        template += '{url}{t.off}'
        lib.colors.print(template, url=url, space='')
        print()

def print_ts_lines(fp):
    template = '{t.dim}{m:02}:{s:05.2f}{t.off}'
    placeholder = ' ' * len(template.format(m=0, s=0, t=types.SimpleNamespace(dim='', off='')))
    start = None
    for line in fp:
        line = line.rstrip(b'\r\n')
        *pragmas, text = line.split(b'\r')
        have_ts = False
        for pragma in pragmas:
            pragma = re.sub(br'\x1B\[.*?[a-zA-Z]', b'', pragma)
            match = re.match(br'\Atravis_time:end:\w+:start=(\d+),finish=(\d+),duration=\d+\Z', pragma)
            if match is None:
                continue
            if start is None:
                start = int(match.group(1)) / 1E9
            end = int(match.group(2)) / 1E9
            t = end - start
            m, s = divmod(t, 60)
            if have_ts:
                print()
            ts = lib.colors.format(template, m=int(m), s=s)
            print(ts, end='')
            have_ts = True
        if have_ts:
            print('', end=' ')
        else:
            print(placeholder, end=' ')
        sys.stdout.flush()
        sys.stdout.buffer.write(text)
        sys.stdout.buffer.write(b'\n')

@dispatch(r'jobs/(?P<job_id>\d+)')
def show_job(options, project, job_id):
    url = f'https://api.travis-ci.org/jobs/{job_id}/log.txt'
    with get(url) as fp:
        if options.raw_cr:
            shutil.copyfileobj(fp, sys.stdout.buffer)
        elif options.timestamps:
            print_ts_lines(fp)
        else:
            for line in fp:
                line = line.rstrip(b'\r\n').rsplit(b'\r', 1)[-1]
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.write(b'\n')

__all__ = ['main']

# vim:ts=4 sts=4 sw=4 et
