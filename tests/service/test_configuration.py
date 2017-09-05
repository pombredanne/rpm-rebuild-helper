"""Tests for the service configuration mechanism"""

from collections import namedtuple

import pytest

from rpmrh.service import configuration


class Registered:
    """Test class for registration"""

    # Name in the registries
    type_name = 'registered'

    def __init__(self, name):
        self.name = name


# configurations for Registered instance
registered_configuration = pytest.mark.parametrize('service_configuration', [
    {'type': Registered.type_name, 'name': 'configured'},
])


@pytest.fixture
def registry():
    """Fresh configuration registry"""

    return dict()


@pytest.fixture
def filled_registry(registry):
    """Configuration registry with expected contents"""

    configuration.register(Registered.type_name, registry=registry)(Registered)

    assert Registered.type_name in registry
    assert registry[Registered.type_name] is Registered

    return registry


@pytest.fixture
def service_index():
    """Empty service index"""

    return configuration.Index('test_key_set')


def test_register_simple(filled_registry):
    """Class using __init__ to configure can be registered."""

    instance = filled_registry[Registered.type_name]('test')

    assert isinstance(instance, Registered)
    assert instance.name == 'test'


def test_register_custom_initializer(registry):
    """Class using custom initializer can be registered."""

    @configuration.register('test', initializer='from_test', registry=registry)
    class Test:
        def __init__(self, identification):
            self.identification = identification

        @classmethod
        def from_test(cls, original):
            return cls(original * 2)

    assert 'test' in registry

    instance = registry['test']('reg')

    assert isinstance(instance, Test)
    assert instance.identification == 'regreg'


def test_double_registration_fails(registry):
    """Second registration of class type raises exception"""

    @configuration.register('test', registry=registry)
    class A:
        pass

    with pytest.raises(KeyError):
        @configuration.register('test', registry=registry)
        class B:
            pass


def test_invalid_initializer_fails(registry):
    """Non-existent initializer is reported."""

    with pytest.raises(AttributeError):
        @configuration.register('test', initializer='none', registry=registry)
        class Test:
            pass


@registered_configuration
def test_instantiate_make_instance(service_configuration, filled_registry):
    """Registered type can be instantiated indirectly."""

    instance = configuration.instantiate(
        service_configuration,
        registry=filled_registry,
    )

    assert instance
    assert isinstance(instance, Registered)
    assert instance.name == service_configuration['name']


@registered_configuration
def test_instantiate_raises_unknown(service_configuration, registry):
    """Exception is raised on unknown type."""

    with pytest.raises(KeyError):
        configuration.instantiate(service_configuration, registry=registry)


def test_index_inserts_matched(service_index):
    """Matching service is indexed."""

    matching = namedtuple('Matching', ['test_key_set'])({'test', 'key'})

    inserted = service_index.insert(matching)

    assert inserted is matching
    assert all(key in service_index for key in matching.test_key_set)
    assert service_index[matching.test_key_set.pop()] is matching


def test_index_pass_unmatched(service_index):
    """Mismatching service is passed without exception."""

    mismatching = namedtuple('Mismatching', ['key_set'])({'test', 'key'})

    passed = service_index.insert(mismatching)

    assert passed is mismatching
    assert all(key not in service_index for key in mismatching.key_set)
