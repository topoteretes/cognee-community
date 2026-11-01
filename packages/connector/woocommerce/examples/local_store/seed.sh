#!/usr/bin/env sh
# Set up a local WooCommerce store with a little realistic data, and print a
# read-only REST API key for the connector.
#
#   cd examples/local_store
#   docker compose up -d
#   sh seed.sh
#
# Everything runs through WP-CLI in the `cli` container. The store is at
# http://localhost:8088 (admin / admin).
set -eu

wp() { docker compose exec -T cli wp --user=admin "$@"; }
id_of() { wp "$@" --porcelain; }

# The database and WordPress need a few seconds on the first start.
until docker compose exec -T cli wp core is-installed >/dev/null 2>&1 \
    || docker compose exec -T cli wp db check >/dev/null 2>&1; do sleep 2; done

if ! docker compose exec -T cli wp core is-installed >/dev/null 2>&1; then
  docker compose exec -T cli wp core install --url=http://localhost:8088 \
    --title="Cognee Test Store" --admin_user=admin --admin_password=admin \
    --admin_email=admin@example.com --skip-email
fi
# Pretty permalinks: the REST API lives under /wp-json/.
docker compose exec -T cli wp rewrite structure '/%postname%/' >/dev/null
docker compose exec -T cli wp plugin install woocommerce --activate

shirts=$(id_of wc product_cat create --name="Shirts")
mugs=$(id_of wc product_cat create --name="Mugs")

linen=$(id_of wc product create --name="Linen Shirt" --type=variable --sku=LINEN-01 \
  --categories="[{\"id\":$shirts}]" \
  --short_description="<p>Breathable linen for hot days.</p>" \
  --description="<p>Pre-washed European linen. <strong>Runs small</strong>: most customers size up.</p><ul><li>Machine wash cold</li><li>Do not tumble dry</li></ul>" \
  --attributes='[{"name":"Size","options":["S","M","L"],"variation":true,"visible":true}]')
for size in S M L; do
  wp wc product_variation create "$linen" --regular_price=40 \
    --attributes="[{\"name\":\"Size\",\"option\":\"$size\"}]" --sku="LINEN-01-$size" >/dev/null
done

mug=$(id_of wc product create --name="Stoneware Mug" --type=simple --sku=MUG-01 \
  --regular_price=18 --sale_price=15 --categories="[{\"id\":$mugs}]" \
  --short_description="<p>Hand-glazed, 350 ml.</p>" \
  --description="<p>Each mug is glazed by hand, so colours vary a little. Dishwasher safe.</p>")

wp wc product_review create "$linen" --rating=4 --name="Asha" --email=asha@example.com \
  --review="Lovely fabric, but it runs small. I had to exchange my M for an L." >/dev/null
wp wc product_review create "$linen" --rating=3 --name="Ben" --email=ben@example.com \
  --review="Shipping took almost two weeks to reach me." >/dev/null
wp wc product_review create "$mug" --rating=5 --name="Chen" --email=chen@example.com \
  --review="Perfect size for my morning coffee." >/dev/null

customer=$(id_of wc customer create --email=dana@example.com --first_name=Dana --last_name=Shah \
  --billing='{"city":"Pune","country":"IN","company":"Shah Studio"}')

order=$(id_of wc shop_order create --customer_id="$customer" --status=processing \
  --customer_note="Please gift wrap, it's a birthday present." \
  --line_items="[{\"product_id\":$mug,\"quantity\":2}]")
wp wc order_note create "$order" --note="Gift wrapping added; ships tomorrow." \
  --customer_note=true >/dev/null
# A private note signed by the admin, as a note typed in the admin is (WP-CLI's
# order_note command cannot set added_by_user, so this goes through PHP).
docker compose exec -T cli wp --user=admin eval \
  "wc_get_order($order)->add_order_note('Customer called to confirm the address.', 0, true);"

wp wc shop_order create --status=completed \
  --line_items="[{\"product_id\":$mug,\"quantity\":1}]" >/dev/null

# A read-only REST API key for the connector (WooCommerce > Settings > Advanced
# > REST API creates the same thing in the admin).
docker compose exec -T cli wp eval '
global $wpdb;
$ck = "ck_" . wc_rand_hash();
$cs = "cs_" . wc_rand_hash();
$wpdb->insert($wpdb->prefix . "woocommerce_api_keys", array(
  "user_id" => 1, "description" => "cognee connector", "permissions" => "read",
  "consumer_key" => wc_api_hash($ck), "consumer_secret" => $cs,
  "truncated_key" => substr($ck, -7),
));
echo "WOOCOMMERCE_STORE_URL=http://localhost:8088\n";
echo "WOOCOMMERCE_CONSUMER_KEY=$ck\n";
echo "WOOCOMMERCE_CONSUMER_SECRET=$cs\n";'
