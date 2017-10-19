"""Command Line Interface for the package"""

import logging
from collections import defaultdict, OrderedDict
from contextlib import ExitStack
from functools import reduce, wraps
from itertools import chain, repeat
from operator import attrgetter
from pathlib import Path
from typing import Callable, Iterator, Iterable, Mapping, TextIO
from typing import Optional, Union

import attr
import click
import toml
from attr.validators import instance_of
from ruamel import yaml

from . import RESOURCE_ID, configuration, util, rpm
from .service.abc import Repository, Builder, BuildFailure


@attr.s(slots=True, frozen=True, cmp=False)
class CollectionSequence:
    """A container for loading, dumping and iterating over a stream of SCLs."""

    #: Inner data structure; two-level mapping: el -> collection -> packages
    structure = attr.ib(validator=instance_of(Mapping))

    @attr.s(slots=True, frozen=True, cmp=False)
    class Item:
        """Single iteration item"""

        #: The EL version of a collection
        el = attr.ib(validator=instance_of(int))
        #: The name of a collection
        collection = attr.ib(validator=instance_of(str))
        #: An iterable of packages
        #: associated with the collection and EL version
        package_iter = attr.ib(validator=instance_of(Iterable))

        def __iter__(self):
            """Tuple-like iteration (enables unpacking)"""

            return iter(attr.astuple(self, recurse=False))

    def __iter__(self):
        """Iterate over current structure"""

        for el, collection_map in self.structure.items():
            for collection, packages in collection_map.items():
                yield self.Item(el, collection, packages)

    @classmethod
    def consume(cls, iterator: Iterator[Item]) -> 'CollectionSequence':
        """Create new CollectionSequence from consumed iterator."""

        structure = {}

        for item in iterator:
            collection_map = structure.setdefault(item.el, {})
            collection_map[item.collection] = sorted(item.package_iter)

        return cls(structure)

    @classmethod
    def from_yaml(cls, stream: Union[TextIO, str]) -> 'CollectionSequence':
        """Create new CollectionSequence from YAML."""

        # TODO: Validation
        # TODO: Package conversion str->rpm.Metadata
        return cls(yaml.safe_load(stream))

    def to_yaml(self, stream: Optional[TextIO] = None) -> Optional[str]:
        """Dump current contents into YAML."""

        def represent_metadata(dumper: yaml.Dumper, metadata: rpm.Metadata):
            return dumper.represent_str(str(metadata))
        yaml.SafeDumper.add_multi_representer(rpm.Metadata, represent_metadata)

        return yaml.safe_dump(
            self.structure, stream,
            default_flow_style=False,
        )

    # Command decorators

    @staticmethod
    def processor(func: Callable):
        """A decorator for commands processing `CollectionSequence.Item`s."""

        @wraps(func)
        def wrapper(*args, **kwargs):  # will receive arguments for func
            @wraps(func)
            def bound(iterator: Iterator[CollectionSequence.Item]):
                """Bind args and kwargs, "wait" for the iterator"""

                return func(iterator, *args, **kwargs)

            return bound
        return wrapper


@attr.s(slots=True, frozen=True)
class RunParameters:
    """Global run parameters (CLI options, etc.)"""

    #: Source group name
    source = attr.ib(validator=instance_of(str))
    #: Destination group name
    destination = attr.ib(validator=instance_of(str))
    #: EL major version
    el = attr.ib(validator=instance_of(int))

    #: Configured and indexed service instances
    service = attr.ib(validator=instance_of(configuration.service.Registry))

    @service.default
    def load_user_and_bundled_services(_self):
        """Loads all available user and bundled service configuration files."""

        streams = chain(
            util.open_resource_files(
                root_dir='conf.d',
                extension='.service.toml',
            ),
            util.open_config_files(
                extension='.service.toml',
            ),
        )

        with ExitStack() as opened:
            streams = map(opened.enter_context, streams)
            contents = map(toml.load, streams)

            return configuration.service.Registry.from_merged(*contents)


# Logging setup
logger = logging.getLogger(RESOURCE_ID)
util.logging.basic_config(logger)


# Commands
@click.group(chain=True)
@util.logging.quiet_option(logger)
@click.option(
    '--from', '-f', 'source',
    help='Name of a source group (tag, target, ...).'
)
@click.option(
    '--to', '-t', 'destination',
    help='Name of a destination group (tag, target, ...).'
)
@click.option(
    '--el', '-e', type=click.IntRange(6), default=7,
    help='Major EL version.',
)
@click.option(
    '--collection', '-c', 'collection_seq', multiple=True,
    help='Name of the SCL to work with (can be used multiple times).'
)
@click.option(
    '--report', type=click.File(mode='w', encoding='utf-8'),
    default='-',
    help='File name of the final report [default: stdout].',
)
@click.pass_context
def main(context, **options):
    """RPM Rebuild Helper – an automation tool for mass RPM rebuilding,
    with focus on Software Collections.
    """

    # Store configuration
    config_fields = {field.name for field in attr.fields(RunParameters)}
    context.obj = RunParameters(**{
        k: v for k, v in options.items() if k in config_fields
    })


@main.resultcallback()
@click.pass_context
def run_chain(
    context,
    processor_seq,
    collection_seq,
    report,
    **_config_options,
):
    """Run a sequence of collections through a processor sequence.

    Keyword arguments:
        processor_seq: The callables to apply to the collection sequence.
        collection_seq: The sequence of SCL names to be processed.
        report: The file to write the result report into.
    """

    collection_seq = CollectionSequence({
        context.params['el']: dict(zip(collection_seq, repeat([]))),
    })

    # Apply the processors
    pipeline = reduce(
        lambda data, proc: proc(data),
        processor_seq,
        collection_seq
    )

    # Output the results in YAML format
    CollectionSequence.consume(pipeline).to_yaml(stream=report)


@main.command()
@CollectionSequence.processor
@click.pass_obj
def diff(params, collection_stream):
    """List all packages from source tag missing in destination tag."""

    for scl in collection_stream:
        def latest_builds(group):
            """Fetch latest builds from a group."""

            tag = params.service.unalias('tag', group, attr.asdict(scl))
            repo = params.service.find('tag', tag, type=Repository)

            yield from repo.latest_builds(tag)

        # Packages present in destination
        present = {
            build.name: build
            for build in latest_builds(params.destination)
            if build.name.startswith(scl.collection)
        }

        def obsolete(package):
            return (
                package.name in present
                and present[package.name] >= package
            )

        missing = (
            pkg for pkg in latest_builds(params.source)
            if pkg.name.startswith(scl.collection)
            and not obsolete(pkg)
        )

        yield attr.evolve(scl, package_iter=missing)


@main.command()
@click.option(
    '--output-dir', '-d', metavar='DIR',
    type=click.Path(file_okay=False, writable=True),
    default='.',
    help='Target directory for downloaded packages [default: "."].'
)
@CollectionSequence.processor
@click.pass_obj
def download(params, collection_stream, output_dir):
    """Download packages into specified directory."""

    log = logger.getChild('download')
    output_dir = Path(output_dir).resolve()

    for scl in collection_stream:
        tag = params.service.unalias('tag', params.source, attr.asdict(scl))
        repo = params.service.find('tag', tag, type=Repository)
        collection_dir = output_dir / scl.collection

        collection_dir.mkdir(exist_ok=True)

        def logged_download(package):
            log.info('Fetching {}'.format(package))
            return repo.download(package, collection_dir)

        paths = map(logged_download, scl.package_iter)

        yield attr.evolve(scl, package_iter=paths)


@main.command()
@click.option(
    '--failed', '-f', 'fail_file',
    type=click.File(mode='w', encoding='utf-8', lazy=True),
    help='Path to store build failures to [default: stderr].',
)
@CollectionSequence.processor
@click.pass_obj
def build(params, collection_stream, fail_file):
    """Attempt to build packages in target."""

    failed = defaultdict(set)

    for scl in collection_stream:
        target = params.service.unalias(
            'target', params.destination, attr.asdict(scl),
        )
        builder = params.service.find('target', target, type=Builder)

        def build_and_filter_failures(packages):
            with builder:
                for pkg in packages:
                    try:
                        yield builder.build(target, pkg)
                    except BuildFailure as failure:
                        failed[scl.collection].add(failure)

        built = build_and_filter_failures(scl.package_iter)
        yield attr.evolve(scl, package_iter=built)

    if not failed:
        raise StopIteration()

    # Convert the stored exceptions to readable representation
    readable_failures = {
        scl: OrderedDict(
            (f.package.nevra, f.reason)
            for f in sorted(fails, key=attrgetter('package'))
        ) for scl, fails in failed.items()
    }

    if fail_file is None:
        fail_file = click.get_text_stream('stderr', encoding='utf-8')

    yaml.dump(
        readable_failures,
        stream=fail_file,
        default_flow_style=False,
        default_style='>',
    )
