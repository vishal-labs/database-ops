-- Fake enterprise data: 11 tables, FK-heavy, join-friendly
DROP TABLE IF EXISTS shipments, payments, inventory, order_items, orders,
  products, suppliers, warehouses, customers, employees, departments CASCADE;

CREATE TABLE departments (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  location TEXT NOT NULL,
  budget NUMERIC NOT NULL
);

CREATE TABLE employees (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  dept_id INT NOT NULL REFERENCES departments(id),
  manager_id INT REFERENCES employees(id),
  salary NUMERIC NOT NULL,
  hired_at DATE NOT NULL
);

CREATE TABLE suppliers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT NOT NULL,
  rating NUMERIC NOT NULL
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  price NUMERIC NOT NULL,
  supplier_id INT NOT NULL REFERENCES suppliers(id)
);

CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  country TEXT NOT NULL,
  tier TEXT NOT NULL,
  signup_at DATE NOT NULL
);

CREATE TABLE warehouses (
  id SERIAL PRIMARY KEY,
  location TEXT NOT NULL,
  capacity INT NOT NULL
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INT NOT NULL REFERENCES customers(id),
  ordered_at DATE NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE order_items (
  id SERIAL PRIMARY KEY,
  order_id INT NOT NULL REFERENCES orders(id),
  product_id INT NOT NULL REFERENCES products(id),
  quantity INT NOT NULL,
  unit_price NUMERIC NOT NULL
);

CREATE TABLE payments (
  id SERIAL PRIMARY KEY,
  order_id INT NOT NULL REFERENCES orders(id),
  amount NUMERIC NOT NULL,
  method TEXT NOT NULL,
  paid_at DATE NOT NULL
);

CREATE TABLE inventory (
  id SERIAL PRIMARY KEY,
  product_id INT NOT NULL REFERENCES products(id),
  warehouse_id INT NOT NULL REFERENCES warehouses(id),
  stock INT NOT NULL
);

CREATE TABLE shipments (
  id SERIAL PRIMARY KEY,
  order_id INT NOT NULL REFERENCES orders(id),
  warehouse_id INT NOT NULL REFERENCES warehouses(id),
  carrier TEXT NOT NULL,
  shipped_at DATE NOT NULL
);

-- Fake data (generate_series keeps this short but rows join cleanly)

INSERT INTO departments (name, location, budget)
SELECT 'Dept ' || g,
       (ARRAY['NYC','London','Berlin','Tokyo','Remote'])[1 + (g % 5)],
       100000 + g * 25000
FROM generate_series(1, 5) g;

INSERT INTO employees (name, dept_id, manager_id, salary, hired_at)
SELECT 'Emp ' || g,
       1 + (g % 5),
       CASE WHEN g % 10 = 0 THEN NULL ELSE 1 + (g % 9) END,
       50000 + (g * 137 % 80000),
       DATE '2019-01-01' + (g % 2500)
FROM generate_series(1, 100) g;
UPDATE employees SET manager_id = NULL WHERE manager_id >= id;

INSERT INTO suppliers (name, country, rating)
SELECT 'Supplier ' || g,
       (ARRAY['US','DE','CN','IN','JP'])[1 + (g % 5)],
       2.5 + (g % 26) / 10.0
FROM generate_series(1, 20) g;

INSERT INTO products (name, category, price, supplier_id)
SELECT 'Product ' || g,
       (ARRAY['Electronics','Furniture','Clothing','Food','Toys'])[1 + (g % 5)],
       10 + (g * 7 % 490),
       1 + (g % 20)
FROM generate_series(1, 50) g;

INSERT INTO customers (name, email, country, tier, signup_at)
SELECT 'Customer ' || g,
       'customer' || g || '@example.com',
       (ARRAY['US','DE','IN','BR','JP'])[1 + (g % 5)],
       (ARRAY['basic','silver','gold'])[1 + (g % 3)],
       DATE '2022-01-01' + (g % 1300)
FROM generate_series(1, 200) g;

INSERT INTO warehouses (location, capacity)
SELECT (ARRAY['NYC','LA','Berlin','Tokyo'])[1 + (g % 4)],
       10000 * g
FROM generate_series(1, 4) g;

INSERT INTO orders (customer_id, ordered_at, status)
SELECT 1 + (g % 200),
       DATE '2024-01-01' + (g % 600),
       (ARRAY['pending','shipped','delivered','cancelled'])[1 + (g % 4)]
FROM generate_series(1, 1000) g;

INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT o.id,
       1 + ((o.id * 3) % 50),
       1 + (o.id % 5),
       p.price
FROM orders o
JOIN products p ON p.id = 1 + ((o.id * 3) % 50);

INSERT INTO payments (order_id, amount, method, paid_at)
SELECT o.id,
       sum(oi.quantity * oi.unit_price),
       (ARRAY['card','paypal','wire'])[1 + (o.id % 3)],
       o.ordered_at + 3
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
GROUP BY o.id, o.ordered_at, o.status
HAVING (ARRAY['pending','shipped','delivered','cancelled'])[1 + (o.id % 4)] <> 'cancelled';

INSERT INTO inventory (product_id, warehouse_id, stock)
SELECT p.id, w.id, (p.id * w.id * 17) % 500
FROM products p CROSS JOIN warehouses w;

INSERT INTO shipments (order_id, warehouse_id, carrier, shipped_at)
SELECT o.id,
       1 + (o.id % 4),
       (ARRAY['DHL','FedEx','UPS'])[1 + (o.id % 3)],
       o.ordered_at + 5
FROM orders o
WHERE o.status IN ('shipped', 'delivered');
