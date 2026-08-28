monikrab's Contract Library
===========================

A faster Python contracts library, meant for use directly in production. Inspired by life4/deal and deadpixi/contracts.

Benchmark vs. deadpixi/contracts::

    ====== dpcontracts vs libcon ========
    
    @require        100,000 calls: 6.5502s
    @pre            100,000 calls: 0.0457s

    @ensure         100,000 calls: 7.5970s
    @post           100,000 calls: 0.0473s

    @types          100,000 calls: 6.7278s
    @typed          100,000 calls: 0.0919s

    @invariant      100,000 calls: 0.0537s
    @inv            100,000 calls: 0.0367s


Features
--------

.. code-block :: python
    
    @pre("<msg>", lambda _: ...)

    @post("<msg>", lambda _, _: ...)

    @inv("<msg>", lambda self: ...)

    @typed
    def <f>(<var>: <T>) -> <T>:
        ...

    @raises(<Exception>)

    chain(@<con>, @<con>, ...)
