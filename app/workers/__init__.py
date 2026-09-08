"""Background work that runs inside the app, on no user's request.

    reaper.py   sweeps conversations whose device job nobody is watching any more

A worker is started by the app's lifespan (main.py) and cancelled on shutdown. The rule
for this package: a worker owns SCHEDULING only -- when to run, how often, what to do
about a failure -- and calls the same service methods an endpoint calls. No business
logic here, or the background path and the request path would drift apart, and only one
of them would be tested.
"""
