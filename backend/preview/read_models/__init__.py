"""Only explicitly reviewed read models are installed."""


def register_all(repository):
    from . import monitoring,acquisition,dynagraphs
    monitoring.register(repository)
    acquisition.register(repository)
    dynagraphs.register(repository)
