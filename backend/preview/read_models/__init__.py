"""Only explicitly reviewed read models are installed."""


def register_all(repository):
    from . import monitoring,acquisition,dynagraphs,studies
    monitoring.register(repository)
    acquisition.register(repository)
    dynagraphs.register(repository)
    studies.register(repository)
