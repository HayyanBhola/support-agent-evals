-- GlowCare Seed Data (FICTIONAL DEMO DATA)
-- Product rows mirror data/products/products.json exactly on purpose —
-- that JSON file is the source of truth used to generate this seed.

INSERT INTO products (product_id, name, description, price, currency, stock_quantity, availability) VALUES
('GC-P001', 'Hydra Balance Moisturizer', 'A lightweight, oil-free daily moisturizer designed to hydrate without clogging pores.', 24.99, 'USD', 42, 'in_stock'),
('GC-P002', 'Barrier Repair Cream', 'A rich, ceramide-based cream formulated to support and restore the skin''s natural moisture barrier.', 32.50, 'USD', 17, 'in_stock'),
('GC-P003', 'Niacinamide Serum', 'A 10% niacinamide serum aimed at improving the appearance of uneven texture and visible pores.', 19.99, 'USD', 0, 'out_of_stock'),
('GC-P004', 'Gentle Daily Cleanser', 'A sulfate-free, low-pH cleanser that removes impurities without stripping the skin.', 16.00, 'USD', 65, 'in_stock'),
('GC-P005', 'Lightweight Sunscreen SPF 50', 'A broad-spectrum SPF 50 sunscreen with a light, non-greasy finish.', 21.00, 'USD', 8, 'in_stock')
ON CONFLICT (product_id) DO NOTHING;

-- Full ingredient lists (Phase 5). Kept as UPDATEs so re-running is safe.
UPDATE products SET ingredients = ARRAY['Water', 'Glycerin', 'Hyaluronic Acid', 'Niacinamide', 'Dimethicone', 'Phenoxyethanol'] WHERE product_id = 'GC-P001';
UPDATE products SET ingredients = ARRAY['Water', 'Ceramide NP', 'Cholesterol', 'Fatty Acids', 'Shea Butter', 'Squalane'] WHERE product_id = 'GC-P002';
UPDATE products SET ingredients = ARRAY['Water', 'Niacinamide 10%', 'Zinc PCA', 'Glycerin', 'Panthenol'] WHERE product_id = 'GC-P003';
UPDATE products SET ingredients = ARRAY['Water', 'Coco-Glucoside', 'Glycerin', 'Panthenol', 'Allantoin'] WHERE product_id = 'GC-P004';
UPDATE products SET ingredients = ARRAY['Water', 'Zinc Oxide', 'Titanium Dioxide', 'Glycerin', 'Dimethicone'] WHERE product_id = 'GC-P005';

INSERT INTO customers (name, email) VALUES
('Sara Ahmed', 'sara.ahmed@example.com'),
('Bilal Khan', 'bilal.khan@example.com'),
('Ayesha Raza', 'ayesha.raza@example.com')
ON CONFLICT (email) DO NOTHING;

-- Orders reference customers by email lookup so this script is idempotent
-- even if customer_id auto-increment values differ between runs.

INSERT INTO orders (order_id, customer_id, status, total_amount)
SELECT 'GC10241', customer_id, 'shipped', 24.99
FROM customers WHERE email = 'sara.ahmed@example.com'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO orders (order_id, customer_id, status, total_amount)
SELECT 'GC10242', customer_id, 'processing', 32.50
FROM customers WHERE email = 'bilal.khan@example.com'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO orders (order_id, customer_id, status, total_amount)
SELECT 'GC10243', customer_id, 'delivered', 37.00
FROM customers WHERE email = 'ayesha.raza@example.com'
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO order_items (order_id, product_id, quantity, price_at_purchase) VALUES
('GC10241', 'GC-P001', 1, 24.99),
('GC10242', 'GC-P002', 1, 32.50),
('GC10243', 'GC-P004', 1, 16.00),
('GC10243', 'GC-P005', 1, 21.00);
