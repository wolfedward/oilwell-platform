class MonitoringError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def invalid(field: str) -> MonitoringError:
    return MonitoringError('monitoring.invalid_query', f'查询字段 {field} 无效或超出范围')
