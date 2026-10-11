"""Input contract failures, separate from business and numerical failures."""


class InversionContractError(ValueError):
    def __init__(self, code: str = "contract_invalid"):
        self.code = code
        super().__init__(code)
