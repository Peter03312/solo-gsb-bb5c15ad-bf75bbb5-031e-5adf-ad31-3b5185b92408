"""预留/路由/排队域：/reservation-simulate、/route-simulate 与 /queue-simulate 的模型。"""
from __future__ import annotations

from app.models.scheduling.queueing import (
    QueueSimRequest,
    QueueSimResult,
    QueueSimulateRequest,
    QueueSimulateResponse,
)
from app.models.scheduling.reservation import (
    OccupancyInterval,
    ReservationBucket,
    ReservationBucketOccupancy,
    ReservationRequest,
    ReservationResult,
    ReservationSimulateRequest,
    ReservationSimulateResponse,
)
from app.models.scheduling.routing import (
    RouteCandidateRejection,
    RouteSimRequest,
    RouteSimResult,
    RouteSimulateRequest,
    RouteSimulateResponse,
)

__all__ = [
    "OccupancyInterval",
    "QueueSimRequest",
    "QueueSimResult",
    "QueueSimulateRequest",
    "QueueSimulateResponse",
    "ReservationBucket",
    "ReservationBucketOccupancy",
    "ReservationRequest",
    "ReservationResult",
    "ReservationSimulateRequest",
    "ReservationSimulateResponse",
    "RouteCandidateRejection",
    "RouteSimRequest",
    "RouteSimResult",
    "RouteSimulateRequest",
    "RouteSimulateResponse",
]
