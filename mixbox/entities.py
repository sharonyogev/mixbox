# Copyright (c) 2015, The MITRE Corporation. All rights reserved.
# See LICENSE.txt for complete terms.

import collections
import inspect
import json

from . import idgen, dates, xml
from .binding_utils import save_encoding
from .datautils import is_sequence
from .fields import TypedField, DateTimeField, DateField, CDATAField
from .namespaces import Namespace, lookup_name, lookup_prefix
from .namespaces import get_xmlns_string, get_schemaloc_string
from .vendor import six


def _objectify(field, value, ns_info):
    """Make `value` suitable for a binding object.

    If `value` is an Entity, call to_obj() on it. Otherwise, return it
    unmodified.
    """
    if hasattr(value, "to_obj"):
        return value.to_obj(ns_info=ns_info)
    elif isinstance(field, CDATAField):
        return xml.cdata(value)
    else:
        return value


def _dictify(field, value):
    """Make `value` suitable for a dictionary.

    * If `value` is an Entity, call to_dict() on it.
    * If value is a timestamp, turn it into a string value.
    * If none of the above are satisfied, return the input value
    """
    if hasattr(value, "to_dict"):
        return value.to_dict()
    elif isinstance(field, DateTimeField):
        return dates.serialize_datetime(value)
    elif isinstance(field, DateField):
        return dates.serialize_date(value)
    else:
        return value


class EntityFactory(object):
    _dictkey   = "xsi:type"
    _objkey    = "xsi_type"

    @classmethod
    def entity_class(cls, key):
        """Must be implemented by a subclass."""
        pass

    @classmethod
    def from_dict(cls, cls_dict):
        if not cls_dict:
            return None

        typekey = cls_dict.get(cls._dictkey)
        klass   = cls.entity_class(typekey)
        return klass.from_dict(cls_dict)

    @classmethod
    def from_obj(cls, cls_obj):
        if not cls_obj:
            return None

        typekey = getattr(cls_obj, cls._objkey, None)
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
        self._typed_fields = None

    @classmethod
    def _iter_typed_fields(cls):
        is_field = lambda x: isinstance(x, TypedField)

        for name, field in inspect.getmembers(cls, predicate=is_field):
            yield field

    @property
    def typed_fields(self):
        """Return a list of this entity's TypedFields."""
        if self._typed_fields is None:
            self._typed_fields = list(self._iter_typed_fields())

        return self._typed_fields

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

        # If there are no TypedFields, assume this class hasn't been
        # "TypedField"-ified, so we don't want these to inadvertently return
        # equal.
        if not self.typed_fields:
            return False

        for f in self.typed_fields:
            if not f.comparable:
                continue
            if f.__get__(self) != f.__get__(other):
                return False

        return True

    def __ne__(self, other):
        return not self == other


    def _collect_ns_info(self, ns_info=None):
        if not ns_info:
            return
        ns_info.collect(self)


    def to_obj(self, ns_info=None):
        """Convert to a GenerateDS binding object.

        Subclasses can override this function.

        Returns:
            An instance of this Entity's ``_binding_class`` with properties
            set from this Entity.
        """
        self._collect_ns_info(ns_info)

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

        for field in entity.typed_fields:
            val = getattr(cls_obj, field.name)

            if field.transformer:
                if field.multiple and val is not None:
                    val = [field.transformer.from_obj(x) for x in val]
                else:
                    val = field.transformer.from_obj(val)

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

        for field in entity.typed_fields:
            val = cls_dict.get(field.key_name)
            if field.transformer:
                if field.multiple:
                    if val is not None:
                        val = [field.transformer.from_dict(x) for x in val]
                    else:
                        val = []
                else:
                    val = field.transformer.from_dict(val)
            else:
                if field.multiple and not val:
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
            elif isinstance(v, list):
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
    _contained_type = object

    # Don't try to cast list types (yet)
    _try_cast = False

    # To use as a key if we want to represent the EntityList as a dictionary.
    # If None or empty string, to_dict() will return a list.
    _inner_name = None

    def __init__(self, *args):
        super(EntityList, self).__init__()
        self._inner = []

        if not any(args):
            return

        for arg in args:
            if is_sequence(arg):
                self.extend(arg)
            else:
                self.append(arg)

    def __nonzero__(self):
        return bool(self._inner)

    def __getitem__(self, key):
        return self._inner.__getitem__(key)

    def __setitem__(self, key, value):
        if not self._is_valid(value):
            value = self._fix_value(value)
        self._inner.__setitem__(key, value)

    def __delitem__(self, key):
        self._inner.__delitem__(key)

    def __len__(self):
        return len(self._inner)

    def insert(self, idx, value):
        if not value:
            return
        if not self._is_valid(value):
            value = self._fix_value(value)
        self._inner.insert(idx, value)

    def _is_valid(self, value):
        """Check if this is a valid object to add to the list.

        Subclasses can override this function, but it's probably better to
        modify the istypeof function on the _contained_type.
        """
        return self._contained_type.istypeof(value)

    def _fix_value(self, value):
        """Attempt to coerce value into the correct type.

        Subclasses can override this function.
        """
        try:
            new_value = self._contained_type(value)
        except:
            error = "Can't put '{0}' ({1}) into a {2}. Expected a {3} object."
            error = error.format(
                value,                  # Input value
                type(value),            # Type of input value
                type(self),             # Type of collection
                self._contained_type    # Expected type of input value
            )
            raise ValueError(error)

        return new_value

    # The next four functions can be overridden, but otherwise define the
    # default behavior for EntityList subclasses which define the following
    # class-level members:
    # - _binding_class
    # - _binding_var
    # - _contained_type
    # - _inner_name

    def to_obj(self, ns_info=None):
        obj = super(EntityList, self).to_obj(ns_info=ns_info)
        tmplist = [x.to_obj(ns_info=ns_info) for x in self]
        setattr(obj, self._binding_var, tmplist)
        return obj

    def to_list(self):
        return [h.to_dict() for h in self]

    def to_dict(self):
        if not self._inner_name:
            return self.to_list()

        d = super(EntityList, self).to_dict()

        if self._inner:
            d[self._inner_name] = [x.to_dict() for x in self]

        return d

    @classmethod
    def from_dict(cls, cls_dict):
        if not cls_dict:
            return None

        if not cls._inner_name:
            return cls.from_list(cls_dict)

        obj = super(EntityList, cls).from_dict(cls_dict)

        if cls._inner_name in cls_dict:
            obj.extend(cls.from_list(cls_dict[cls._inner_name]))

        return obj

    @classmethod
    def from_obj(cls, list_obj):
        if not list_obj:
            return None

        entitylist = super(EntityList, cls).from_obj(list_obj)

        for item in getattr(list_obj, cls._binding_var):
            entitylist.append(cls._contained_type.from_obj(item))

        return entitylist

    @classmethod
    def from_list(cls, seq):
        if not seq:
            return None

        entitylist = cls()

        for item in seq:
            entitylist.append(cls._contained_type.from_dict(item))

        return entitylist

    @classmethod
    def object_from_list(cls, entitylist_list):
        """Convert from list representation to object representation."""
        return cls.from_list(entitylist_list).to_obj()

    @classmethod
    def list_from_object(cls, entitylist_obj):
        """Convert from object representation to list representation."""
        return cls.from_obj(entitylist_obj).to_list()
