# -*- coding=utf-8 -*-
from __future__ import absolute_import
import sys
from pipenv import PIPENV_VENDOR, PIPENV_PATCHED

sys.path.insert(0, PIPENV_VENDOR)
sys.path.insert(0, PIPENV_PATCHED)
import hashlib
import requirements
import six
from collections import defaultdict
from pip9.index import Link
from pip9.req.req_install import _strip_extras
from pipenv.utils import SCHEME_LIST, VCS_LIST, is_installable_file, is_vcs, multi_split, Path, get_converted_relative_path, is_star, is_pinned
from first import first

HASH_STRING = ' --hash={0}'


class PipenvRequirement(object):
    """Requirement for Pipenv Use

    Provides the following methods:
        - as_pipfile
        - as_lockfile
        - as_requirement
        - from_line
        - from_pipfile
        - resolve
    """
    _editable_prefix = '-e '

    def __init__(
        self,
        name=None,
        path=None,
        uri=None,
        extras=None,
        markers=None,
        editable=False,
        vcs=None,
        link=None,
        requirement=None,
        line=None,
        index=None,
        hashes=[],
    ):
        if not requirement:
            requirement = self._create_requirement(
                name=name,
                path=path,
                uri=uri,
                extras=extras,
                markers=markers,
                editable=editable,
                vcs=vcs,
                link=link,
                line=line,
            )
        self.requirement = requirement
        self.name = getattr(requirement, 'name', name)
        self.path = path or getattr(requirement, 'path', None)
        self.uri = uri or getattr(requirement, 'uri', None)
        self.extras = extras or getattr(requirement, 'extras', None)
        self.markers = markers or getattr(requirement, 'markers', None)
        self.editable = editable or getattr(requirement, 'editable', None)
        self.vcs = vcs or getattr(requirement, 'vcs', None)
        self.link = link or getattr(requirement, 'link', None)
        self.line = line or getattr(requirement, 'line', None)
        self.index = index
        self.hashes = hashes

    @property
    def original_line(self):
        _editable = ''
        if self.editable:
            _editable += self._editable_prefix
        if self.line and (self.path or self.uri):
            # original_line adds in -e if necessary
            if self.line.startswith(self._editable_prefix):
                return self.line

            return '{0}{1}'.format(_editable, self.line)

        return self.constructed_line

    @property
    def constructed_line(self):
        _editable = ''
        if self.editable:
            _editable += self._editable_prefix
        line = ''
        if self.link:
            line = '{0}{1}'.format(_editable, self.link.url)
        elif self.path or self.uri:
            line = '{0}{1}'.format(_editable, self.path or self.uri)
        else:
            line += self.name
        if not self.vcs:
            line = '{0}{1}{2}{3}{4}'.format(
                line,
                self.extras_as_pip,
                self.specifiers_as_pip,
                self.markers_as_pip,
                self.hashes_as_pip,
            )
        else:
            line = self.line
            if _editable == self._editable_prefix and not self.line.startswith(
                _editable
            ):
                line = '{0}{1}'.format(_editable, self.line)
            line = '{0}{1}{2}'.format(
                line, self.markers_as_pip, self.hashes_as_pip
            )
        return line

    @property
    def extras_as_pip(self):
        if self.extras:
            return '[{0}]'.format(','.join(self.extras))

        return ''

    @property
    def markers_as_pip(self):
        if self.markers:
            return '; {0}'.format(self.markers)

        return ''

    @property
    def specifiers_as_pip(self):
        if self.requirement.specs:
            return ','.join([''.join(spec) for spec in self.requirement.specs])

        return ''

    @property
    def hashes_as_pip(self):
        if self.hashes:
            if isinstance(self.hashes, six.string_types):
                return HASH_STRING.format(self.hashes)

            return ''.join([HASH_STRING.format(h) for h in self.hashes])

        return ''

    @classmethod
    def from_pipfile(cls, name, indexes, pipfile_entry):
        if is_star(pipfile_entry) or isinstance(
            pipfile_entry, six.string_types
        ):
            version = '' if (
                str(pipfile_entry) == '{}' or is_star(pipfile_entry)
            ) else pipfile_entry
            return PipenvRequirement(name='{0}{1}'.format(name, version))

        hashes = None
        line = None
        if 'hashes' in pipfile_entry or 'hash' in pipfile_entry:
            hashes = pipfile_entry.get('hashes', pipfile_entry.get('hash'))
        editable = True if pipfile_entry.get('editable') else False
        vcs = first([vcs for vcs in VCS_LIST if vcs in pipfile_entry])
        uri = pipfile_entry.get('uri')
        extras = pipfile_entry.get('extras')
        link = None
        if vcs:
            vcs_uri = pipfile_entry.get(vcs)
            vcs_ref = pipfile_entry.get('ref')
            vcs_subdirectory = pipfile_entry.get('subdirectory')
            vcs_line = build_vcs_link(
                vcs,
                vcs_uri,
                name=name,
                extras=extras,
                ref=vcs_ref,
                subdirectory=vcs_subdirectory,
                editable=False,
            )
            _editable = cls._editable_prefix if editable else ''
            line = '{0}{1}'.format(_editable, vcs_line)
            link = Link(vcs_line)
            uri = _clean_git_uri(vcs_line)
        if pipfile_entry.get('version'):
            name = '{0}{1}'.format(name, pipfile_entry.get('version'))
        return PipenvRequirement(
            name=name,
            path=pipfile_entry.get('path'),
            uri=uri,
            markers=pipfile_entry.get('markers'),
            extras=extras,
            index=pipfile_entry.get('index'),
            hashes=hashes,
            vcs=vcs,
            editable=editable,
            link=link,
            line=line,
        )

    @classmethod
    def from_line(cls, line):
        """Pre-clean requirement strings passed to the requirements parser.

        Ensures that we can accept both local and relative paths, file and VCS URIs,
        remote URIs, and package names, and that we pass only valid requirement strings
        to the requirements parser. Performs necessary modifications to requirements
        object if the user input was a local relative path.

        :param str dep: A requirement line
        :returns: :class:`requirements.Requirement` object
        """
        hashes = None
        if '--hash=' in line:
            hashes = line.split(' --hash=')
            line, hashes = hashes[0], hashes[1:]
        editable = False
        _editable = ''
        if line.startswith('-e '):
            editable = True
            _editable += cls._editable_prefix
            line = line.split(' ', 1)[1]
        line, markers = cls._split_markers(line)
        line, extras = _strip_extras(line)
        req_dict = defaultdict(None)
        vcs = None
        if is_installable_file(line):
            req_dict = cls._prep_path(line)
        elif is_vcs(line):
            req_dict = cls._prep_vcs(line)
            vcs = first(
                _split_vcs_method(
                    req_dict.get('uri', req_dict.get('path', line))
                )
            )
            req_dict['original_line'] = '{0}{1}'.format(
                _editable, req_dict['original_line']
            )
        else:
            req_dict = {
                'line': line,
                'original_line': line,
                'name': multi_split(line, '!=<>~')[0],
            }
        return PipenvRequirement(
            line=req_dict['original_line'],
            name=req_dict.get('name'),
            path=req_dict.get('path'),
            uri=req_dict.get('uri'),
            link=req_dict.get('link'),
            hashes=hashes,
            markers=markers,
            extras=extras,
            editable=editable,
            vcs=vcs,
        )

    def as_pipfile(self):
        """"Converts a requirement to a Pipfile-formatted one."""
        req_dict = {}
        req = self.requirement
        req_dict = {}
        if req.local_file:
            hashable_path = req.uri or req.path
            dict_key = 'file' if req.uri else 'path'
            hashed_path = hashlib.sha256(
                hashable_path.encode('utf-8')
            ).hexdigest(
            )
            req_dict[dict_key] = hashable_path
            req_dict['name'] = hashed_path[
                len(hashed_path) - 7:
            ] if not req.vcs else req.name
        elif req.vcs:
            if req.name is None:
                raise ValueError(
                    'pipenv requires an #egg fragment for version controlled '
                    'dependencies. Please install remote dependency '
                    'in the form {0}#egg=<package-name>.'.format(req.uri)
                )

            if req.uri and req.uri.startswith('{0}+'.format(req.vcs)):
                if req_dict.get('uri'):
                    # req_dict['uri'] = req.uri[len(req.vcs) + 1:]
                    del req_dict['uri']
                req_dict.update(
                    {
                        req.vcs: req.uri[
                            len(req.vcs) + 1:
                        ] if req.uri else req.path
                    }
                )
            if req.subdirectory:
                req_dict.update({'subdirectory': req.subdirectory})
            if req.revision:
                req_dict.update({'ref': req.revision})
        elif req.specs:
            # Comparison operators: e.g. Django>1.10
            specs = ','.join([''.join(spec) for spec in req.specs])
            req_dict.update({'version': specs})
        else:
            req_dict.update({'version': '*'})
        if self.extras:
            req_dict.update({'extras': self.extras})
        if req.editable:
            req_dict.update({'editable': req.editable})
        if self.hashes:
            hash_key = 'hashes'
            hashes = self.hashes
            if isinstance(hashes, six.string_types) or len(hashes) == 1:
                hash_key = 'hash'
                if len(hashes) == 1:
                    hashes = first(hashes)
            req_dict.update({hash_key: hashes})
        if len(req_dict.keys()) == 1 and req_dict.get('version'):
            return {req.name: req_dict.get('version')}

        return {req.name: req_dict}

    def as_requirement(self, project=None, include_index=False):
        """Creates a requirements.txt compatible output of the current dependency.

        :param project: Pipenv Project, defaults to None
        :param project: :class:`pipenv.project.Project`, optional
        :param include_index: Whether to include the resolved index, defaults to False
        :param include_index: bool, optional
        """
        line = self.constructed_line
        if include_index and not (self.local_file or self.vcs):
            from .utils import prepare_pip_source_args

            if self.index:
                pip_src_args = [project.get_source(self.index)]
            else:
                pip_src_args = project.sources
            index_string = ' '.join(prepare_pip_source_args(pip_src_args))
            line = '{0} {1}'.format(line, index_string)
        return line

    @staticmethod
    def _split_markers(line):
        """Split markers from a dependency"""
        if not any(line.startswith(uri_prefix) for uri_prefix in SCHEME_LIST):
            marker_sep = ';'
        else:
            marker_sep = '; '
        markers = None
        if marker_sep in line:
            line, markers = line.split(marker_sep, 1)
            markers = markers.strip() if markers else None
        return line, markers

    @staticmethod
    def _prep_path(line):
        _path = Path(line)
        link = Link(_path.absolute().as_uri())
        if _path.is_absolute() or _path.as_posix() == '.':
            path = _path.as_posix()
        else:
            path = get_converted_relative_path(line)
        name_or_url = link.egg_fragment if link.egg_fragment else link.url_without_fragment
        name = link.egg_fragment or link.show_url or link.filename
        return {
            'link': link,
            'path': path,
            'line': name_or_url,
            'original_line': line,
            'name': name,
        }

    @staticmethod
    def _prep_vcs(line):
        # Generate a Link object for parsing egg fragments
        link = Link(line)
        # Save the original path to store in the pipfile
        original_uri = link.url
        # Construct the requirement using proper git+ssh:// replaced uris or names if available
        formatted_line = _clean_git_uri(line)
        return {
            'link': link,
            'uri': formatted_line,
            'line': original_uri,
            'original_line': line,
            'name': link.egg_fragment,
        }

    @classmethod
    def _create_requirement(
        cls,
        line=None,
        name=None,
        path=None,
        uri=None,
        extras=None,
        markers=None,
        editable=False,
        vcs=None,
        link=None,
    ):
        _editable = cls._editable_prefix if editable else ''
        _line = line or uri or path or name
        # We don't want to only use the name on properly
        # formatted VCS inputs
        if vcs or is_vcs(_line):
            _line = uri or path or line
            _line = '{0}{1}'.format(_editable, _line)
        req = first(requirements.parse(_line))
        req.line = _line
        if editable:
            req.editable = True
        if req.name and not any(
            getattr(req, prop) for prop in ['uri', 'path']
        ):
            if link and link.scheme.startswith('file') and path:
                req.path = path
                req.local_file = True
            elif link and uri:
                req.uri = link.url_without_fragment
        elif req.local_file and path and not req.vcs:
            req.uri = None
            req.path = path
        elif req.vcs and not req.local_file and uri != link.url:
            req.uri = _strip_ssh_from_git_uri(req.uri)
            req.line = line or _strip_ssh_from_git_uri(req.line)
        if markers:
            req.markers = markers
        if extras:
            # Bizarrely this is also what pip does...
            req.extras = first(
                requirements.parse(
                    'fakepkg{0}'.format(_extras_to_string(extras))
                )
            ).extras
        req.link = link
        return req


def _strip_ssh_from_git_uri(uri):
    """Return git+ssh:// formatted URI to git+git@ format"""
    if isinstance(uri, six.string_types):
        uri = uri.replace('git+ssh://', 'git+')
    return uri


def _clean_git_uri(uri):
    """Cleans VCS uris from pip9 format"""
    if isinstance(uri, six.string_types):
        # Add scheme for parsing purposes, this is also what pip does
        if uri.startswith('git+') and '://' not in uri:
            uri = uri.replace('git+', 'git+ssh://')
    return uri


def _split_vcs_method(uri):
    """Split a vcs+uri formatted uri into (vcs, uri)"""
    vcs_start = '{0}+'
    vcs = first(
        [vcs for vcs in VCS_LIST if uri.startswith(vcs_start.format(vcs))]
    )
    if vcs:
        vcs, uri = uri.split('+', 1)
    return vcs, uri


def _extras_to_string(extras):
    """Turn a list of extras into a string"""
    if isinstance(extras, six.string_types):
        if extras.startswith('['):
            return extras

        else:
            extras = [extras]
    return '[{0}]'.format(','.join(extras))


def build_vcs_link(
    vcs, uri, name=None, ref=None, subdirectory=None, editable=None, extras=[]
):
    _editable = PipenvRequirement._editable_prefix if editable else ''
    anchor = '{0}{1}+'.format(_editable, vcs)
    if not uri.startswith(anchor):
        uri = '{0}{1}'.format(anchor, uri)
    if ref:
        uri = '{0}@{1}'.format(uri, ref)
    if name:
        uri = '{0}#egg={1}'.format(uri, name)
        if extras:
            extras = _extras_to_string(extras)
            uri = '{0}{1}'.format(uri, extras)
    if subdirectory:
        uri = '{0}&subdirectory={1}'.format(uri, subdirectory)
    return uri


if __name__ == "__main__":
    # line = '-e git+git@github.com:pypa/pipenv.git@master#egg=pipenv'
    # line = 'requests'
    pf = {
        'requests': {
            'extras': ['security'],
            'git': 'https://github.com/requests/requests.git',
            'ref': 'master',
        }
    }
    from pipenv.core import project

    r = PipenvRequirement.from_pipfile(
        'requests', project.sources, pf['requests']
    )
    print(r.as_requirement())
    print(r.requirement.__dict__)
