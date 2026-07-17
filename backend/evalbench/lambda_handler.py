"""Mangum entry point wrapping the FastAPI app for the api Lambda."""

from mangum import Mangum

from evalbench.api.app import app

handler = Mangum(app)
