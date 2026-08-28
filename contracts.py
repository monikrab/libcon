
from ast import parse
from collections import namedtuple
from functools import wraps, lru_cache
from inspect import isfunction, ismethod, iscoroutinefunction, getfullargspec, getsource
from typing import get_origin, get_args, Union



class PreconditionError(AssertionError):
    pass

class PostconditionError(AssertionError):
    pass

class TypeContractError(TypeError):
    pass

class RaisesError(AssertionError):
    pass


_MISSING = object()




def get_function_source(func):
    try:
        source = getsource(func); tree = parse(source)

        decorators = tree.body[0].decorator_list
        function = tree.body[0]
        
        if decorators:
            first_line = decorators[0].lineno
            following_line = first_line + 1

            if len(decorators) > 1:
                following_line = decorators[1].lineno

            elif function.body:
                following_line = function.body[0].lineno - 1

            return "\n".join(source.split("\n")[first_line - 1:following_line - first_line]) + " failed"

        return str(func)

    except (SyntaxError, OSError, IndexError):
        return str(func)



def get_wrapped_func(func):
    while hasattr(func, "__contract_wrapped_func__"):
        func = func.__contract_wrapped_func__
    
    return func



def arg_count(func):
    spec = _get_spec(func)

    return len(spec.args) + len(spec.kwonlyargs) + (1 if spec.varargs else 0)



@lru_cache(maxsize=None)
def _get_spec(func):
    return getfullargspec(func)



@lru_cache(maxsize=None)
def _get_args_class(func):
    spec = _get_spec(func)
    
    if spec.varkw:
        return None, None
    
    fields = tuple(spec.args) + ((spec.varargs,) if spec.varargs else ()) + tuple(spec.kwonlyargs)
    field_to_idx = {name: i for i, name in enumerate(fields)}

    if fields:
        params = ",".join(fields)
        assignments = ";".join(f"self.{name}={name}" for name in fields)
        init_source = f"def __init__(self,{params}): {assignments}"

    else:
        init_source = "def __init__(self): pass"

    namespace = {"__slots__": fields, "_fields": fields}
    exec(init_source, {}, namespace)

    def getitem(self, index):
        if isinstance(index, slice):
            return tuple(getattr(self, name) for name in fields[index])

        return getattr(self, fields[index])


    def iterate(self):
        for name in fields:
            yield getattr(self, name)


    def replace(self, **kwargs):
        if any(key not in field_to_idx for key in kwargs):
            raise ValueError(f"Unexpected fields: {set(kwargs) - set(field_to_idx)}")

        values = [kwargs.get(name, getattr(self, name)) for name in fields]

        return type(self)(*values)


    namespace.update({
        "__getitem__": getitem,
        "__iter__": iterate,
        "__len__": lambda self: len(fields),
        "_asdict": lambda self: {name: getattr(self, name) for name in fields},
        "_replace": replace,
    })
    return type("Args", (), namespace), fields



@lru_cache(maxsize=None)
def _get_call_info(func):
    spec = _get_spec(func)

    args_class, field_names = _get_args_class(func)

    if args_class is None:
        return None

    name_to_idx = {name: i for i, name in enumerate(field_names)}
    defaults = [_MISSING] * len(field_names)

    if spec.defaults:
        start_index = len(spec.args) - len(spec.defaults)
        for index, value in enumerate(spec.defaults, start_index):
            defaults[index] = value

    if spec.kwonlydefaults:
        for name, value in spec.kwonlydefaults.items():
            defaults[name_to_idx[name]] = value

    vararg_idx = name_to_idx[spec.varargs] if spec.varargs else None

    return args_class, tuple(defaults), name_to_idx, len(spec.args), vararg_idx, field_names



@lru_cache(maxsize=None)
def _get_binder(func):
    info = _get_call_info(func)

    if info is not None:
        args_class, defaults, name_to_idx, positional_count, vararg_idx, field_names = info
        field_count = len(field_names)

        def generic(args, kwargs):
            values = list(defaults)
            num_args = min(len(args), positional_count)
            values[:num_args] = args[:num_args]

            if vararg_idx is not None:
                values[vararg_idx] = tuple(args[positional_count:])

            for name, value in kwargs.items():
                if name in name_to_idx:
                    values[name_to_idx[name]] = value

            for i, value in enumerate(values):
                if value is _MISSING:
                    raise TypeContractError(f"{func.__name__} missing required argument: '{field_names[i]}'")

            return args_class(*values)

        if vararg_idx is None and field_count == positional_count and all(value is _MISSING for value in defaults):
            values_str = ",".join(f"args[{i}]" for i in range(positional_count))

            source = f"def bind(args,kwargs):\n    if not kwargs and len(args)=={positional_count}: return Args({values_str})\n    return generic(args,kwargs)"
            namespace = {"Args": args_class, "generic": generic}

            exec(source, namespace)

            return namespace["bind"]

        return generic

    def fallback_binder(args, kwargs):
        spec = _get_spec(func)
        nonce = object()
        actual = {}

        for name in spec.args:
            actual[name] = nonce

        defs = spec.defaults or ()

        kwonlydefs = spec.kwonlydefaults or {}

        actual.update(kwonlydefs)
        actual.update(dict(zip(reversed(spec.args), reversed(defs))))
        actual.update(dict(zip(spec.args, args)))

        if spec.varargs is not None:
            actual[spec.varargs] = tuple(args[len(spec.args):])
        actual.update(kwargs)

        for name, value in actual.items():
            if value is nonce:
                raise TypeContractError(f"{func.__name__} missing required argument: '{name}'")

        return tuple_of_dict(actual)

    return fallback_binder



@lru_cache(maxsize=None)
def _get_type_checks(func):
    spec = _get_spec(func)

    annotations = getattr(func, "__annotations__", {})

    info = _get_call_info(func)
    if info is None:
        return ()

    _, _, name_to_idx, *_ = info
    return tuple((name_to_idx[name], annotations[name]) for name in spec.args + spec.kwonlyargs if name in annotations)



def _check_type(value, expected):
    origin = get_origin(expected)

    if origin is Union:
        return any(_check_type(value, arg_type) for arg_type in get_args(expected))

    return isinstance(value, origin) if origin is not None else isinstance(value, expected)



def build_call(func, wrapped=None, *args, **kwargs):
    if wrapped is None:
        wrapped = get_wrapped_func(func)

    binder = _get_binder(wrapped)

    return binder(args, kwargs) if binder else None



def tuple_of_dict(dictionary, name="Args"):
    assert isinstance(dictionary, dict)

    return namedtuple(name, dictionary.keys())(**dictionary)



def condition(description, predicate, precondition=False, postcondition=False, instance=False):
    assert isinstance(description, str) and description, "contract descriptions must be nonempty strings"

    assert isfunction(predicate) and not iscoroutinefunction(predicate), "predicates must be sync functions"

    assert precondition or postcondition, "must be at least one"

    predicate_arity = arg_count(predicate)
    if instance or precondition:
        assert predicate_arity == 1
    elif postcondition:
        assert predicate_arity in (2, 3)

    def require(func):
        wrapped = get_wrapped_func(func)
        type_checks = () if instance else _get_type_checks(wrapped)

        preservers = tuple(getattr(wrapped, "__contract_preserver__", ()))
        has_preservers = bool(preservers)

        three_arg_post = postcondition and predicate_arity == 3

        is_async = iscoroutinefunction(func)

        binder = None if instance else _get_binder(wrapped)


        def check_args(args, kwargs, do_precondition):
            bound_args = args[0] if instance else binder(args, kwargs)

            if not instance and type_checks:
                for idx, expected_type in type_checks:
                    if not _check_type(bound_args[idx], expected_type):
                        raise TypeContractError(f"{func.__name__} argument at position {idx} has invalid type")

            if do_precondition and not predicate(bound_args):
                raise PreconditionError(description)

            return bound_args


        def post_check(bound_args, result, preserved_values):
            if instance:
                if not predicate(bound_args):
                    raise PostconditionError(description)

            elif postcondition:
                check_passed = predicate(bound_args, result, tuple_of_dict(preserved_values)) if three_arg_post else predicate(bound_args, result)

                if not check_passed:
                    raise PostconditionError(description)


        if is_async:
            @wraps(func)
            async def inner(*args, **kwargs):
                bound_args = check_args(args, kwargs, precondition)

                preserved_values = {k: v for p in preservers for k, v in p(bound_args).items()} if has_preservers else {}

                result = await func(*args, **kwargs)

                post_check(bound_args, result, preserved_values)

                return result

        else:
            @wraps(func)
            def inner(*args, **kwargs):
                bound_args = check_args(args, kwargs, precondition)

                preserved_values = {k: v for p in preservers for k, v in p(bound_args).items()} if has_preservers else {}

                result = func(*args, **kwargs)

                post_check(bound_args, result, preserved_values)

                return result

        inner.__contract_wrapped_func__ = wrapped

        return inner

    return require



def pre(arg1, arg2=None):
    assert (isinstance(arg1, str) and isfunction(arg2)) or (isfunction(arg1) and arg2 is None)

    description, predicate = (arg1, arg2) if isinstance(arg1, str) else (get_function_source(arg1), arg1)

    return condition(description, predicate, True, False)



def post(arg1, arg2=None):
    assert (isinstance(arg1, str) and isfunction(arg2)) or (isfunction(arg1) and arg2 is None)

    description, predicate = (arg1, arg2) if isinstance(arg1, str) else (get_function_source(arg1), arg1)

    return condition(description, predicate, False, True)



def typed(func):
    return pre(lambda args: True)(func)



def invariant(arg1, arg2=None):
    if isinstance(arg1, str):
        description, predicate = arg1, arg2

    else:
        description, predicate = get_function_source(arg1), arg1

    def decorate(cls):
        dunder_exceptions = {"__getitem__", "__setitem__", "__lt__", "__le__", "__eq__", "__ne__", "__gt__", "__ge__", "__init__"}


        def should_wrap(name, func):
            if name.startswith("__") and name.endswith("__") and name not in dunder_exceptions:
                return False

            if not ismethod(func) and not isfunction(func):
                return False

            if getattr(func, "__self__", None) is cls:
                return False

            return True


        class InvariantContractor(cls):
            pass


        for name in dir(cls):
            value = getattr(cls, name)
            if should_wrap(name, value):
                setattr(InvariantContractor, name, condition(description, predicate, name != "__init__", True, True)(value))

        return InvariantContractor

    return decorate



def raises(*exceptions):
    def decorate(func):
        @wraps(func)
        def inner(*args, **kwargs):
            try:
                return func(*args, **kwargs)

            except BaseException as exc:
                if not isinstance(exc, exceptions):
                    allowed = ", ".join(exc_type.__name__ for exc_type in exceptions)
                    raise RaisesError(f"{func.__name__} raised {type(exc).__name__}, but only {allowed} are allowed") from None
                raise

        return inner

    return decorate



def preserve(preserver):
    assert isfunction(preserver) and arg_count(preserver) == 1


    def decorate(func):
        wrapped = get_wrapped_func(func)

        @wraps(func)
        def inner(*args, **kwargs):
            return func(*args, **kwargs)

        existing = getattr(wrapped, "__contract_preserver__", None)

        if existing is None:
            wrapped.__contract_preserver__ = [preserver]

        else:
            existing.append(preserver)

        return inner

    return decorate



def transform(transformer):
    assert isfunction(transformer) and arg_count(transformer) == 1


    def decorate(func):
        @wraps(func)
        def inner(*args, **kwargs):
            wrapped = get_wrapped_func(func)
            bound_args = transformer(build_call(func, wrapped, *args, **kwargs))

            return func(**bound_args._asdict())

        return inner

    return decorate



def rewrite(args, **kwargs):
    return args._replace(**kwargs)



def compose(*decorators):
    def decorate(func):
        for decorator in reversed(decorators):
            func = decorator(func)

        return func

    return decorate
