"""Bridge profile operations onto the original console's lock and environment."""
from .runtime_config import RuntimeConfigStore
from .service import ConsoleService


class LocalRuntimeConfigStore(RuntimeConfigStore):
    def __init__(self, state):
        super().__init__(state.config_path, base_dir=state.root)
        self.state = state

    def _validate(self, raw):
        return self.state.validate(raw, self.state.environment())[0]


class LocalProfileService(ConsoleService):
    def __init__(self, state):
        super().__init__(state.config_path, base_dir=state.root,
                         summaries_dir=state.summaries, runner=self)
        self.state = state
        self.config = LocalRuntimeConfigStore(state)
        # All profile writes, routing writes, legacy saves and starts serialize.
        self._lock = state.lock

    def is_running(self):
        return self.state.run["status"] in {"running", "stopping"}
