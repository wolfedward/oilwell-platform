"""Only explicitly reviewed read models are installed."""


def register_all(repository):
    from . import monitoring
    monitoring.register(repository)
