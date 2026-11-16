"""Register the Stripe Connect integration with cognee's integrations API.

Import this module in the process that serves cognee's API, before the app
starts handling requests::

    import cognee_community_connector_stripe.register  # noqa: F401
    from cognee.api.client import app

The generic ``/api/v1/integrations/stripe/*`` routes then serve the Stripe
Connect flow, the object-type picker and sync. Importing the connector
package alone never registers anything.
"""

from cognee.modules.integrations.registry import use_integration

from cognee_community_connector_stripe.integration import StripeIntegration

use_integration(StripeIntegration())
