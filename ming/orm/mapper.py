from ming.base import Object
from ming.utils import wordwrap
from ming.metadata import Field

from .base import ObjectState, state
from .icollection import InstrumentedObj
from .property import FieldProperty

def mapper(cls, collection=None, session=None, **kwargs):
    if collection is None and session is None:
        if isinstance(cls, type):
            return Mapper.by_class(cls)
        elif isinstance(cls, basestring):
            return Mapper.by_classname(cls)
        else:
            return Mapper._mapper_by_class[cls.__class__]
    return Mapper(cls, collection, session, **kwargs)

class Mapper(object):
    _mapper_by_collection = {}
    _mapper_by_class = {}
    _mapper_by_classname = {}


    def __init__(self, mapped_class, collection, session, **kwargs):
        self.mapped_class = mapped_class
        self.collection = collection
        self.session = session
        self.properties = []
        self._mapper_by_collection[collection] = self
        self._mapper_by_class[mapped_class] = self
        classname = '%s.%s' % (mapped_class.__module__, mapped_class.__name__)
        self._mapper_by_classname[classname] = self
        properties = kwargs.pop('properties', {})
        include_properties = kwargs.pop('include_properties', None)
        exclude_properties = kwargs.pop('exclude_properties', [])
        if kwargs:
            raise TypeError, 'Unknown kwd args: %r' % kwargs
        self._instrument_class(properties, include_properties, exclude_properties)

    def __repr__(self):
        return '<Mapper %s:%s>' % (
            self.mapped_class.__name__, self.collection.m.collection_name)

    def insert(self, obj, state, **kwargs):
        doc = self.collection(state.document, skip_from_bson=True)
        doc.m.insert(**kwargs)
        self.session.save(obj)
        state.status = state.clean

    def update(self, obj, state, **kwargs):
        doc = self.collection(state.document, skip_from_bson=True)
        doc.m.save(**kwargs)
        self.session.save(obj)
        state.status = state.clean

    def delete(self, obj, state, **kwargs):
        doc = self.collection(state.document, skip_from_bson=True)
        doc.m.delete(**kwargs)
        self.session.expunge(obj)

    def remove(self, *args, **kwargs):
        self.collection.m.remove(*args, **kwargs)

    def create(self, doc):
        doc = self.collection.make(doc)
        mapper = self.by_collection(type(doc))
        return mapper._from_doc(doc)

    @classmethod
    def by_collection(cls, collection_class):
        return cls._mapper_by_collection[collection_class]

    @classmethod
    def by_class(cls, mapped_class):
        return cls._mapper_by_class[mapped_class]

    @classmethod
    def by_classname(cls, name):
        try:
            return cls._mapper_by_classname[name]
        except KeyError:
            for n, mapped_class in cls._mapper_by_classname.iteritems():
                if n.endswith('.' + name): return mapped_class
            raise

    def _from_doc(self, doc):
        obj = self.mapped_class.__new__(self.mapped_class)
        obj.__ming__ = _ORMDecoration(self, obj)
        st = state(obj)
        st.document = doc
        st.status = st.new
        self.session.save(obj)
        return obj
    
    def update_partial(self, *args, **kwargs):
        self.collection.m.update_partial(*args, **kwargs)

    def _instrument_class(self, properties, include_properties, exclude_properties):
        self.mapped_class.query = _QueryDescriptor(self)
        base_properties = dict((fld.name, fld) for fld in self.collection.m.fields)
        properties = dict(base_properties, **properties)
        if include_properties:
            properties = dict((k,properties[k]) for k in include_properties)
        for k in exclude_properties:
            properties.pop(k, None)
        for k,v in properties.iteritems():
            v.name = k
            if isinstance(v, Field):
                v = FieldProperty(v)
            v.mapper = self
            setattr(self.mapped_class, k, v)
            self.properties.append(v)
        _InitDecorator.decorate(self.mapped_class, self)
        inst = self._instrumentation()
        for k in ('__repr__', '__getitem__', '__setitem__', '__contains__',
                  'delete'):
            if getattr(self.mapped_class, k, ()) == getattr(object, k, ()):
                setattr(self.mapped_class, k, getattr(inst, k).im_func)

    def _instrumentation(self):
        class _Instrumentation(object):
            def __repr__(self_):
                properties = [
                    '%s=%s' % (prop.name, prop.repr(self_))
                    for prop in self.properties
                    if prop.include_in_repr ]
                return wordwrap(
                    '<%s %s>' % 
                    (self_.__class__.__name__, ' '.join(properties)),
                    60,
                    indent_subsequent=2)
            def delete(self_):
                self_.query.delete()
            def __getitem__(self_, name):
                try:
                    return getattr(self_, name)
                except AttributeError:
                    raise KeyError, name
            def __setitem__(self_, name, value):
                setattr(self_, name, value)
            def __contains__(self_, name):
                return hasattr(self_, name)
        return _Instrumentation


class _ORMDecoration(object):

    def __init__(self, mapper, instance):
        self.mapper = mapper
        self.instance = instance
        self.state = ObjectState()
        tracker = _DocumentTracker(self.state)
        self.state.document = InstrumentedObj(tracker)
        self.state.raw = Object()

class _QueryDescriptor(object):

    def __init__(self, mapper):
        self.classquery = _ClassQuery(mapper)

    def __get__(self, instance, cls=None):
        if instance is None: return self.classquery
        else: return _InstQuery(self.classquery, instance)

class _ClassQuery(object):
    _proxy_methods = (
        'find', 'find_and_modify', 'remove', 'update' )

    def __init__(self, mapper):
        self.mapper = mapper
        self.session = self.mapper.session
        self.mapped_class = self.mapper.mapped_class

        def _proxy(name):
            def inner(*args, **kwargs):
                method = getattr(self.session, name)
                return method(self.mapped_class, *args, **kwargs)
            inner.__name__ = name
            return inner

        for method_name in self._proxy_methods:
            setattr(self, method_name, _proxy(method_name))

    def get(self, **kwargs):
        return self.find(kwargs).first()

    def find_by(self, **kwargs):
        return self.find(kwargs)
    

class _InstQuery(object):
    _proxy_methods = (
        'update_if_not_modified',
        )

    def __init__(self, classquery, instance):
        self.classquery = classquery
        self.mapper = classquery.mapper
        self.session = classquery.session
        self.mapped_class = classquery.mapped_class
        self.instance = instance

        def _proxy(name):
            def inner(*args, **kwargs):
                method = getattr(self.session, name)
                return method(self.instance, *args, **kwargs)
            inner.__name__ = name
            return inner

        for method_name in self._proxy_methods:
            setattr(self, method_name, _proxy(method_name))
        self.find = self.classquery.find

    def delete(self):
        st = state(self.instance)
        st.status = st.deleted

class _DocumentTracker(object):
    __slots__ = ('state',)

    def __init__(self, state):
        self.state = state
        self.state.tracker = self

    def soil(self, value):
        self.state.soil()
    added_item = soil
    removed_item = soil
    cleared = soil

class _InitDecorator(object):

    def __init__(self, mapper, func):
        self.mapper = mapper
        self.func = func

    def __get__(self, self_, cls=None):
        if self_ is None: return self
        def __init__(*args, **kwargs):
            self_.__ming__ = _ORMDecoration(self.mapper, self_)
            self.mapper.session.save(self_)
            self.func(self_, *args, **kwargs)
        return __init__

    @classmethod
    def decorate(cls, mapped_class, mapper):
        old_init = mapped_class.__init__
        if isinstance(old_init, cls):
            mapped_class.__init__ = cls(mapper, old_init.func)
        elif old_init is object.__init__:
            mapped_class.__init__ = cls(mapper, _basic_init)
        else:
            mapped_class.__init__ = cls(mapper, old_init)

def _basic_init(self_, **kwargs):
    for k,v in kwargs.iteritems():
        setattr(self_, k, v)
