"""Unit tests: no network, no models, no running gateway.

Importing this package sandboxes the environment, which every module in it
relies on having happened before it imports an app module.
"""

from tests import context

context.install()
