# Copyright (c) 2015, The MITRE Corporation. All rights reserved.
# See LICENSE.txt for complete terms.

import collections
import json

from . import idgen
from .binding_utils import save_encoding
from .datautils import is_sequence
from .namespaces import Namespace, lookup_name, lookup_prefix
from .namespaces import get_xmlns_string, get_schemaloc_string
from .vendor import six

# Note: Some imports moved to the bottom of this module to resolve circular
# import issues.

def _objectify(field, value, ns_info):
    """Make `value` suitable for a binding object.

    If `value` is an Entity, call to_obj() on it. Otherwise, pass it
    off to the TypedField for an appropriate value.
    """
    if value is None:
        return None
    elif field.type_:
        return value.to_obj(ns_info=ns_info)
    return field.binding_value(value)


def _dictify(field, value):
    """Make `value` suitable for a dictionary.

    * If `value` is an Entity, call to_dict() on it.
    * If value is a timestamp, turn it into a string value.
    * If none of the above are satisfied, return the input value
    """
    if value is None:
        return None
    elif field.type_:
        return value.to_dict()
    return field.dict_value(value)


class EntityFactory(object):
    """The EntityFactory class handles type key => class resolution during
    parse time.

    Generally, this class will use xsi:type as a class lookup key, but there
    may be instances where xsi:type is not be the appropriate key. As such
    this class was designed to be flexible enough to handle multiple class
    resolution scenarios.

    Any of the methods defined below can be overridden for non-standard
    keys or lookup scenarios.

    Attributes:
        _dictkey: The key associated with type information in an Entity
            dictionary. Default is "xsi:type".
        _objkey: The attribute associated with type information in a
            generateDS binding object. Default is "xsi_type"
    """

    _dictkey = "xsi:type"  # dictionary key holding type information
    _objkey  = "xsi_type"  # generateDS object attr holding type information.


    @classmethod
    def entity_class(cls, key):
        """**Abstract** This method take the `key` (e.g., xsi:type value) and
        returns the Entity class associated with it.

        Args:
            key: The class lookup key. Generally this is an xsi:type attribute
                value.

        Returns:
            The class associated with the input `key`.

        Raises:
            This should raise ValueError if no class is associated with the
            input key.
        """
        raise NotImplementedError()

    @classmethod
    def instance(cls, key, *args, **kwargs):
        """Create an instance of the class associated with the `key` (xsi:type)
        and initialize it with the *args and **kwargs.

        Args:
            key: A class lookup key (see entity_class()).

        Returns:
            An instance of the class associated with the `key`.
        """
        klass = cls.entity_class(key)
        return klass(*args, **kwargs)

    @classmethod
    def objkey(cls, obj):
        """Return the class lookup key from the input generateDS object.
        If no type information or lookup keys are found on the binding object
        return None.

        Args:
            obj: A generateDS binding object.

        Returns:
            A class lookup key (string) or None.
        """
        return getattr(obj, cls._objkey, None)

    @classmethod
    def dictkey(cls, mapping):
        """Return the class lookup key from the input dictionary.
        If no type information or lookup keys are found on the binding object
        return None.

        Args:
            mapping: A dictionary representation of an Entity instance.

        Returns:
            A class lookup key (string) or None.
        """
        try:
            return mapping[cls._dictkey]
        except (KeyError, AttributeError):
            return None

    @classmethod
    def from_dict(cls, cls_dict):
        """Parse the dictionary and return an Entity instance.

        This will attempt to extract type information from the input
        dictionary and pass it to entity_class to resolve the correct class
        for the type.

        Args:
            cls_dict: A dictionary representation of an Entity object.

        Returns:
            An Entity instance.
        """
        if not cls_dict:
            return None

        if isinstance(cls_dict, str) or isinstance(cls_dict, unicode):
            return cls_dict

        typekey = cls.dictkey(cls_dict)
        klass   = cls.entity_class(typekey)
        return klass.from_dict(cls_dict)

    @classmethod
    def from_obj(cls, cls_obj):
        """Parse the generateDS object and return an Entity instance.

        This will attempt to extract type information from the input
        object and pass it to entity_class to resolve the correct class
        for the type.

        Args:
            cls_obj: A generateDS object.

        Returns:
            An Entity instance.
        """
        if not cls_obj:
            return None

        typekey = cls.objkey(cls_obj)
        klass   = cls.entity_class(typekey)
        return klass.from_obj(cls_obj)


class Entity(object):
    """Base class for all classes in the Cybox SimpleAPI."""

    # By default (unless a particular subclass states otherwise), try to "cast"
    # invalid objects to the correct class using the constructor. Entity
    # subclasses should either provide a "sane" constructor or set this to
    # False.
    _try_cast = True

    def __init__(self):
        self._fields = {}

    @classmethod
    def typed_fields(cls):
        """Return a tuple of this entity's TypedFields."""

        # Checking cls._typed_fields could return a superclass _typed_fields
        # value. So we check our class __dict__ which does not include
        # inherited attributes.
        klassdict = cls.__dict__

        try:
            return klassdict["_typed_fields"]
        except KeyError:
            fields = cls.typed_fields_with_attrnames()
            cls._typed_fields = tuple(field for _, field in fields)
        return cls._typed_fields

    @classmethod
    def typed_fields_with_attrnames(cls):
        """Return a list of (TypedField attribute name, TypedField object)
        tuples for this Entity.
        """
        # Checking cls._typed_fields could return a superclass _typed_fields
        # value. So we check our class __dict__ which does not include
        # inherited attributes.
        klassdict = cls.__dict__

        try:
            return klassdict["_typed_fields_with_attrnames"]
        except KeyError:
            # No typed_fields set on this Entity yet. Find them and store
            # them in the _typed_fields class attribute.
            typed_fields = tuple(fields.iterfields(cls))
            cls._typed_fields_with_attrnames = typed_fields
        return typed_fields

    def __eq__(self, other):
        # This fixes some strange behavior where an object isn't equal to
        # itself
        if other is self:
            return True

        # I'm not sure about this, if we want to compare exact classes or if
        # various subclasses will also do (I think not), but for now I'm going
        # to assume they must be equal. - GTB
        if self.__class__ != other.__class__:
            return False

        # Get all comparable TypedFields
        typedfields = [f for f in self.typed_fields() if f.comparable]

        # If No comparable TypedFields are found, return False so we don't
        # inadvertantly say they are equal.
        if not typedfields:
            return False

        return all(f.__get__(self) == f.__get__(other) for f in typedfields)

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_obj(self, ns_info=None):
        """Convert to a GenerateDS binding object.

        Subclasses can override this function.

        Returns:
            An instance of this Entity's ``_binding_class`` with properties
            set from this Entity.
        """
        if ns_info:
            ns_info.collect(self)

        entity_obj = self._binding_class()

        for field, val in six.iteritems(self._fields):
            if field.multiple:
                if val:
                    val = [_objectify(field, x, ns_info) for x in val]
                else:
                    val = []
            else:
                val = _objectify(field, val, ns_info)

            setattr(entity_obj, field.name, val)

        self._finalize_obj(entity_obj)
        return entity_obj

    def _finalize_obj(self, entity_obj):
        """Subclasses can define additional items in the binding object.

        `entity_obj` should be modified in place.
        """
        pass

    def to_dict(self):
        """Convert to a ``dict``

        Subclasses can override this function.

        Returns:
            Python dict with keys set from this Entity.
        """
        entity_dict = {}


        for field, val in six.iteritems(self._fields):
            if field.multiple:
                if val:
                    val = [_dictify(field, x) for x in val]
                else:
                    val = []
            else:
                val = _dictify(field, val)

            # Only add non-None objects or non-empty lists
            if val is not None and val != []:
                entity_dict[field.key_name] = val

        self._finalize_dict(entity_dict)

        return entity_dict

    def _finalize_dict(self, entity_dict):
        """Subclasses can define additional items in the dictionary.

        `entity_dict` should be modified in place.
        """
        pass

    @classmethod
    def from_obj(cls, cls_obj):
        if not cls_obj:
            return None

        entity = cls()

        for field in cls.typed_fields():
            val = getattr(cls_obj, field.name)

            # Get the class that will perform the from_obj() call and
            # transform the generateDS binding object into an Entity.
            transformer = field.transformer

            if transformer:
                if field.multiple and val is not None:
                    val = [transformer.from_obj(x) for x in val]
                else:
                    val = transformer.from_obj(val)

            field.__set__(entity, val)
        return entity

    @classmethod
    def from_dict(cls, cls_dict):
        if cls_dict is None:
            return None

        entity = cls()

        # Shortcut if an actual dict is not provided:
        if not isinstance(cls_dict, dict):
            value = cls_dict

            try:
                return cls(value)   # Call the class's constructor
            except TypeError as ex:
                fmt  = "Could not instantiate a %s from a %s: %s"
                args = (cls, type(value), value)
                ex.message = fmt % args
                raise

        for field in cls.typed_fields():
            val = cls_dict.get(field.key_name)

            # Get the class that will perform the from_obj() call and
            # transform the generateDS binding object into an Entity.
            transformer = field.transformer

            if transformer:
                if field.multiple:
                    if val is not None:
                        val = [transformer.from_dict(x) for x in val]
                    else:
                        val = []
                else:
                    val = transformer.from_dict(val)
            elif field.multiple and not val:
                val = []

            # Set the value
            field.__set__(entity, val)

        return entity

    def to_xml(self, include_namespaces=True, namespace_dict=None,
               pretty=True, encoding='utf-8'):
        """Serializes a :class:`Entity` instance to an XML string.

        The default character encoding is ``utf-8`` and can be set via the
        `encoding` parameter. If `encoding` is ``None``, a unicode string
        is returned.

        Args:
            include_namespaces (bool): whether to include xmlns and
                xsi:schemaLocation attributes on the root element. Set to true by
                default.
            namespace_dict (dict): mapping of additional XML namespaces to
                prefixes
            pretty (bool): whether to produce readable (``True``) or compact
                (``False``) output. Defaults to ``True``.
            encoding: The output character encoding. Default is ``utf-8``. If
                `encoding` is set to ``None``, a unicode string is returned.

        Returns:
            An XML string for this
            :class:`Entity` instance. Default character encoding is ``utf-8``.

        """
        namespace_def = ""

        if include_namespaces:
            namespace_def = self._get_namespace_def(namespace_dict)

        if not pretty:
            namespace_def = namespace_def.replace('\n\t', ' ')


        with save_encoding(encoding):
            sio = six.StringIO()
            self.to_obj().export(
                sio.write,
                0,
                namespacedef_=namespace_def,
                pretty_print=pretty
            )

        s = six.text_type(sio.getvalue()).strip()

        if encoding:
            return s.encode(encoding)

        return s

    def to_json(self):
        """Export an object as a JSON String."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_doc):
        """Parse a JSON string and build an entity."""
        try:
            d = json.load(json_doc)
        except AttributeError: # catch the read() error
            d = json.loads(json_doc)

        return cls.from_dict(d)

    def _get_namespace_def(self, additional_ns_dict=None):
        # copy necessary namespaces

        namespaces = self._get_namespaces()

        if additional_ns_dict:
            for ns, prefix in six.iteritems(additional_ns_dict):
                namespaces.update([Namespace(ns, prefix)])

        namespaces.update([idgen._get_generator().namespace])

        # if there are any other namepaces, include xsi for "schemaLocation"
        if namespaces:
            namespaces.update([lookup_prefix('xsi')])

        if not namespaces:
            return ""

        #TODO: Is there a better `key` to use here?
        namespaces = sorted(namespaces, key=six.text_type)

        return ('\n\t' + get_xmlns_string(namespaces) +
                '\n\txsi:schemaLocation="' + get_schemaloc_string(namespaces) +
                '"')

    def _get_namespaces(self, recurse=True):
        nsset = set()

        # Get all _namespaces for parent classes
        namespaces = [x._namespace for x in self.__class__.__mro__
                      if hasattr(x, '_namespace')]

        nsset.update([lookup_name(ns) for ns in namespaces])

        #In case of recursive relationships, don't process this item twice
        self.touched = True
        if recurse:
            for x in self._get_children():
                if not hasattr(x, 'touched'):
                    nsset.update(x._get_namespaces())
        del self.touched

        return nsset

    def _get_children(self):
        #TODO: eventually everything should be in _fields, not the top level
        # of vars()

        members = {}
        members.update(vars(self))
        members.update(self._fields)

        for v in six.itervalues(members):
            if isinstance(v, Entity):
                yield v
            elif is_sequence(v):
                for item in v:
                    if isinstance(item, Entity):
                        yield item

    @classmethod
    def istypeof(cls, obj):
        """Check if `cls` is the type of `obj`

        In the normal case, as implemented here, a simple isinstance check is
        used. However, there are more complex checks possible. For instance,
        EmailAddress.istypeof(obj) checks if obj is an Address object with
        a category of Address.CAT_EMAIL
        """
        return isinstance(obj, cls)

    @classmethod
    def object_from_dict(cls, entity_dict):
        """Convert from dict representation to object representation."""
        return cls.from_dict(entity_dict).to_obj()

    @classmethod
    def dict_from_object(cls, entity_obj):
        """Convert from object representation to dict representation."""
        return cls.from_obj(entity_obj).to_dict()


class EntityList(collections.MutableSequence, Entity):
    """An EntityList is an Entity that behaves like a mutable sequence.

    EntityList implementations must define one multiple TypedField which
    has an Entity subclass type. EntityLists can define other TypedFields
    that are not multiple.

    The MutableSequence methods are used to interact with the multiple
    TypedField.
    """

    # Don't try to cast list types (yet)
    _try_cast = False


    def __init__(self, *args):
        super(EntityList, self).__init__()
        assert self._multiple_field()

        if not any(args):
            return

        for arg in args:
            if is_sequence(arg):
                self.extend(arg)
            else:
                self.append(arg)

    def _any_typedfields(self):
        return any(x for x in six.itervalues(self._fields))

    def __nonzero__(self):
        return bool(self._inner or self._any_typedfields())

    __bool__ = __nonzero__

    def __getitem__(self, key):
        return self._inner.__getitem__(key)

    def __setitem__(self, key, value):
        self._inner.__setitem__(key, value)

    def __delitem__(self, key):
        self._inner.__delitem__(key)

    def __len__(self):
        return self._inner.__len__()

    def insert(self, idx, value):
        if not value:
            return
        self._inner.insert(idx, value)

    @classmethod
    def _dict_as_list(cls):
        """Returns True if the to_dict() and from_dict() methods should
        expect to export/parse lists rather than dicts.

        If there is only one TypedField (a multiple field) defined on the
        EntityList, this will return True. If there is more than one
        TypedField, the to_dict() method will export a dict and not a list.

        Note:
            This can be overridden by subclasses to force this behavior.

        TODO (bworrell):
            Remove this functionality. The to_dict() method can export a
            dict, a list, a string/int/etc., or None. This makes parsing
            complicated. The to_dict() method should probably return a dict
            and maybe None at most.
        """
        return len(cls.typed_fields()) == 1

    @classmethod
    def _multiple_field(cls):
        """Return the "multiple" TypedField associated with this EntityList.

        This also lazily sets the ``_entitylist_multiplefield`` value if it
        hasn't been set yet. This is set to a tuple containing one item because
        if we set the class attribute to the TypedField, we would effectively
        add a TypedField descriptor to the class, which we don't want.

        Raises:
            AssertionError: If there is more than one multiple TypedField
                or the the TypedField type_ is not a subclass of Entity.
        """
        klassdict = cls.__dict__

        try:
            # Checking for cls.entitylist_multifield would return any inherited
            # values, so we check the class __dict__ explicitly.
            return klassdict["_entitylist_multifield"][0]
        except (KeyError, IndexError, TypeError):
            multifield_tuple = tuple(fields.find(cls, multiple=True))
            assert len(multifield_tuple) == 1

            # Make sure that the multiple field actually has an Entity type.
            multifield  = multifield_tuple[0]
            assert issubclass(multifield.type_, Entity)

            # Store aside the multiple field. We wrap it in a tuple because
            # just doing ``cls._entitylist_multifield = multifield`` would
            # assign another TypedField descriptor to this class. We don't
            # want that.
            cls._entitylist_multifield =  multifield_tuple

            # Return the multiple TypedField
            return multifield_tuple[0]

    @property
    def _inner(self):
        """Return the EntityList mutliple TypedField inner collection."""
        return self._multiple_field().__get__(self)

    def to_list(self):
        return [h.to_dict() for h in self]

    def to_dict(self):
        if self._dict_as_list():
            return self.to_list()
        return super(EntityList, self).to_dict()

    @classmethod
    def from_dict(cls, cls_dict):
        if not cls_dict:
            return None

        if cls._dict_as_list():
            return cls.from_list(cls_dict)

        return super(EntityList, cls).from_dict(cls_dict)

    @classmethod
    def from_list(cls, seq):
        if not seq:
            return None

        entitylist  = cls()
        transformer = cls._multiple_field().transformer
        entitylist.extend(transformer.from_dict(x) for x in seq)

        return entitylist

    @classmethod
    def object_from_list(cls, entitylist_list):
        """Convert from list representation to object representation."""
        return cls.from_list(entitylist_list).to_obj()

    @classmethod
    def list_from_object(cls, entitylist_obj):
        """Convert from object representation to list representation."""
        return cls.from_obj(entitylist_obj).to_list()


# Resolve circular imports
from . import fields