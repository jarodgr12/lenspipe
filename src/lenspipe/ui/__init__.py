"""Local web console for lenspipe (NiceGUI).

Entry point: :func:`lenspipe.ui.app.serve`. Every action that runs a stage is a
job submitted through :class:`lenspipe.jobs.JobManager`; the console never runs
a stage in-process.
"""
