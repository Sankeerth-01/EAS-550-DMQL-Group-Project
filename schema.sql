-- Source schema for the Olist subset: customers, sellers, products, orders, order_items.
-- Run once on an empty database:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
-- Parents are created before children, in one transaction, so a failure leaves
-- nothing behind. There are no DROP statements; to start over, recreate the
-- database (README, section Reset).

BEGIN;

CREATE TABLE customers (
    customer_id        CHAR(32)     NOT NULL,
    -- Identifies the person. Olist issues a new customer_id for every order, so
    -- one person can appear on several rows and this column must not be UNIQUE.
    customer_unique_id CHAR(32)     NOT NULL,
    customer_city      VARCHAR(100) NOT NULL,
    customer_state     CHAR(2)      NOT NULL,
    CONSTRAINT pk_customers PRIMARY KEY (customer_id),
    CONSTRAINT ck_customers_customer_id_hex
        CHECK (customer_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_customers_customer_unique_id_hex
        CHECK (customer_unique_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_customers_customer_state_format
        CHECK (customer_state ~ '^[A-Z]{2}$')
);

CREATE TABLE sellers (
    seller_id    CHAR(32)     NOT NULL,
    -- Free text typed by the seller. Some values carry a state suffix or other
    -- noise; there is no reference list to correct against, so no format CHECK.
    seller_city  VARCHAR(100) NOT NULL,
    seller_state CHAR(2)      NOT NULL,
    CONSTRAINT pk_sellers PRIMARY KEY (seller_id),
    CONSTRAINT ck_sellers_seller_id_hex
        CHECK (seller_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_sellers_seller_state_format
        CHECK (seller_state ~ '^[A-Z]{2}$')
);

CREATE TABLE products (
    product_id            CHAR(32) NOT NULL,
    -- Nullable: some listed products were never given a category or measured.
    product_category_name VARCHAR(100),
    product_weight_g      INTEGER,
    product_length_cm     INTEGER,
    product_height_cm     INTEGER,
    product_width_cm      INTEGER,
    CONSTRAINT pk_products PRIMARY KEY (product_id),
    CONSTRAINT ck_products_product_id_hex
        CHECK (product_id ~ '^[0-9a-f]{32}$'),
    -- A zero is not a measurement; ingestion stores it as NULL.
    CONSTRAINT ck_products_weight_positive CHECK (product_weight_g > 0),
    CONSTRAINT ck_products_length_positive CHECK (product_length_cm > 0),
    CONSTRAINT ck_products_height_positive CHECK (product_height_cm > 0),
    CONSTRAINT ck_products_width_positive  CHECK (product_width_cm > 0)
);

CREATE TABLE orders (
    order_id                      CHAR(32)    NOT NULL,
    customer_id                   CHAR(32)    NOT NULL,
    order_status                  VARCHAR(20) NOT NULL,
    -- Local times as published, with no zone, so TIMESTAMP without time zone.
    order_purchase_timestamp      TIMESTAMP   NOT NULL,
    order_approved_at             TIMESTAMP,
    order_delivered_carrier_date  TIMESTAMP,
    order_delivered_customer_date TIMESTAMP,
    order_estimated_delivery_date DATE        NOT NULL,
    CONSTRAINT pk_orders PRIMARY KEY (order_id),
    CONSTRAINT ck_orders_order_id_hex
        CHECK (order_id ~ '^[0-9a-f]{32}$'),
    -- customer_id is issued per order, so each customer row belongs to exactly
    -- one order. UNIQUE makes the relationship one to one.
    CONSTRAINT uq_orders_customer_id UNIQUE (customer_id),
    CONSTRAINT fk_orders_customers
        FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT ck_orders_order_status_known
        CHECK (order_status IN ('created', 'approved', 'invoiced', 'processing',
                                'shipped', 'delivered', 'unavailable', 'canceled')),
    CONSTRAINT ck_orders_approved_not_before_purchase
        CHECK (order_approved_at >= order_purchase_timestamp),
    CONSTRAINT ck_orders_delivered_not_before_purchase
        CHECK (order_delivered_customer_date >= order_purchase_timestamp),
    CONSTRAINT ck_orders_estimate_not_before_purchase
        CHECK (order_estimated_delivery_date >= CAST(order_purchase_timestamp AS DATE))
    -- Deliberately not constrained, because the source holds real exceptions:
    -- the carrier handover is sometimes recorded before the purchase or after
    -- the delivery, and status does not reliably imply which dates are present.
);

CREATE TABLE order_items (
    order_id      CHAR(32)       NOT NULL,
    -- Position of the item inside its order: 1, 2, 3, ...
    order_item_id SMALLINT       NOT NULL,
    product_id    CHAR(32)       NOT NULL,
    seller_id     CHAR(32)       NOT NULL,
    price         DECIMAL(10, 2) NOT NULL,
    freight_value DECIMAL(10, 2) NOT NULL,
    CONSTRAINT pk_order_items PRIMARY KEY (order_id, order_item_id),
    CONSTRAINT fk_order_items_orders
        FOREIGN KEY (order_id) REFERENCES orders (order_id),
    CONSTRAINT fk_order_items_products
        FOREIGN KEY (product_id) REFERENCES products (product_id),
    CONSTRAINT fk_order_items_sellers
        FOREIGN KEY (seller_id) REFERENCES sellers (seller_id),
    CONSTRAINT ck_order_items_order_item_id_min CHECK (order_item_id >= 1),
    CONSTRAINT ck_order_items_price_positive CHECK (price > 0),
    -- Zero freight is allowed: some items ship free.
    CONSTRAINT ck_order_items_freight_not_negative CHECK (freight_value >= 0)
);

COMMIT;
