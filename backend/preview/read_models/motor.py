from ..fixtures.motor import fixture
def register(repository):
    repository.register('GET','/api/preview/views/motor',lambda q,a:fixture())
