"""Transaction coordinator for complete generated Espresso setups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .contracts import ResourceRecord, SetupManifest, SetupRequest
from .registry import BuilderRegistry
from .transaction import GeneratedTransaction


@dataclass(frozen=True)
class SetupResult:
    ok: bool
    message: str = ""
    manifest: Optional[SetupManifest] = None


class SetupOrchestrator:
    def __init__(self):
        self.builders = BuilderRegistry("setup route")

    def register(self, route: str, callback: Callable, *, version: int = 1):
        return self.builders.register(route, callback, version=version, domain="SETUP")

    @staticmethod
    def _manifest(request: SetupRequest, resources) -> SetupManifest:
        resources = tuple(resources or ())
        if any(not isinstance(item, ResourceRecord) for item in resources):
            raise TypeError("Setup builders must return ResourceRecord values")
        ids = [item.resource_id for item in resources]
        if len(ids) != len(set(ids)):
            raise ValueError("Setup builder returned duplicate resource IDs")
        return SetupManifest(
            setup_id=request.setup_id,
            effect_id=request.effect_id,
            recipe_id=request.recipe_id,
            label=request.label,
            resources=resources,
            portability=request.portability,
            metadata=request.metadata,
        )

    def create(self, request: SetupRequest) -> SetupResult:
        try:
            builder = self.builders.require(request.route).callback
            with GeneratedTransaction(request.setup_id) as tx:
                resources = builder(request, tx)
                value = self._manifest(request, resources)
                tx.commit()
            return SetupResult(True, "", value)
        except Exception as exc:
            return SetupResult(False, str(exc), None)

    def replace(self, previous: SetupManifest, request: SetupRequest, *,
                cleanup: Callable[[SetupManifest], None]) -> SetupResult:
        result = self.create(request)
        if not result.ok:
            return result
        try:
            cleanup(previous)
        except Exception as exc:
            return SetupResult(False, "Replacement built but cleanup failed: %s" % exc,
                               result.manifest)
        return result
