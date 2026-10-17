-- Test data for examples/example.py.
-- Run it in Redshift Query Editor v2, or through the Data API (see the README).
--   REDSHIFT_EXAMPLE_TABLE=public.support_tickets
--   REDSHIFT_EXAMPLE_KEY=ticket_id
--   REDSHIFT_EXAMPLE_CURSOR=updated_at

DROP TABLE IF EXISTS public.support_tickets;

CREATE TABLE public.support_tickets (
    ticket_id  INT,
    customer   VARCHAR(64),
    subject    VARCHAR(256),
    priority   VARCHAR(8),
    updated_at TIMESTAMP
);

COMMENT ON TABLE public.support_tickets IS 'Support tickets from the help desk.';
COMMENT ON COLUMN public.support_tickets.priority IS 'p1 = outage, p4 = question';

INSERT INTO public.support_tickets VALUES
    (1, 'Acme',    'SSO login loops',             'p1', '2026-10-01 09:00:00'),
    (2, 'Globex',  'Refund for duplicate charge', 'p3', '2026-10-02 14:30:00'),
    (3, 'Initech', 'Export to CSV times out',     'p2', '2026-10-03 11:15:00');

-- Then, between two syncs, change the data:
--   UPDATE public.support_tickets SET priority = 'p1', updated_at = '2026-10-04 08:00:00'
--     WHERE ticket_id = 3;
--   DELETE FROM public.support_tickets WHERE ticket_id = 2;
