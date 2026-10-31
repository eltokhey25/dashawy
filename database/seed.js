// Seed script: creates a default admin account and optional sample data.
// Usage: node database/seed.js  (or npm run seed)
// Set SEED_SAMPLE=1 to also insert demo customers/technicians/requests.
const path = require('path');
const bcrypt = require('bcryptjs');
const db = require(path.join(__dirname, '..', 'backend', 'db'));
const { assignTechnician } = require(path.join(__dirname, '..', 'backend', 'services', 'assignment'));

const SPECIALTIES = ['plumber', 'electrician', 'ac_technician', 'carpenter', 'painter', 'general_handyman'];

function ensureUser(email, password, role) {
  const existing = db.prepare('SELECT id FROM users WHERE email = ?').get(email);
  if (existing) {
    console.log(`[seed] User already exists: ${email}`);
    return existing.id;
  }
  const hash = bcrypt.hashSync(password, 10);
  const result = db
    .prepare('INSERT INTO users (email, password_hash, role) VALUES (?, ?, ?)')
    .run(email, hash, role);
  console.log(`[seed] Created ${role} account: ${email}`);
  return result.lastInsertRowid;
}

function seedAdmin() {
  // No password is written into this file: the repository is public, and a
  // seeded admin with a known password is a working login for anyone who
  // reads it. Set SEED_ADMIN_PASSWORD to create the account.
  const password = process.env.SEED_ADMIN_PASSWORD;
  if (!password) {
    console.log('[seed] SEED_ADMIN_PASSWORD is not set; skipping the admin account.');
    return;
  }
  ensureUser('admin@fixrequest.com', password, 'admin');

  // The default admin must be a super admin. Every admin endpoint is guarded by an
  // explicit permission check that a super admin passes automatically, so an admin
  // row created with the column defaults (is_super_admin = 0, permissions = '[]')
  // is locked out of the whole admin area — including /admin/dashboard — even
  // though it can log in. Promoted rather than inserted so re-running the seed on
  // an existing database also repairs an admin created by an older version.
  const promoted = db
    .prepare('UPDATE users SET is_super_admin = 1 WHERE email = ? AND is_super_admin = 0')
    .run('admin@fixrequest.com').changes;
  if (promoted) console.log('[seed] Default admin promoted to super admin.');
}

function seedSample() {
  const customers = [
    ['أحمد محمد', '+201001112222', 'مدينة نصر، القاهرة', 30.0444, 31.2357],
    ['سارة علي', '+201002223333', 'الزمالك، القاهرة', 30.0567, 31.2165],
    ['محمود خالد', '+201003334444', 'المعادي، القاهرة', 29.9629, 31.2494],
    ['ليلى حسن', '+201004445555', 'شبرا، القاهرة', 30.0753, 31.2459]
  ];

  customers.forEach(([name, phone, location, lat, lng]) => {
    const userId = ensureUser(
      name === 'أحمد محمد' ? 'ahmed@example.com' : `customer-${Date.now()}@example.com`,
      'customer123',
      'customer'
    );
    const existing = db.prepare('SELECT id FROM customers WHERE user_id = ?').get(userId);
    if (!existing) {
      db.prepare(
        `INSERT INTO customers (user_id, name, phone, location_text, latitude, longitude)
         VALUES (?, ?, ?, ?, ?, ?)`
      ).run(userId, name, phone, location, lat, lng);
      console.log(`[seed] Added customer: ${name}`);
    }
  });

  const technicians = [
    ['ياسر عادل', '+201011112222', 'plumber', 'مدينة نصر، القاهرة', 30.0410, 31.2351],
    ['هاني سمير', '+201022223333', 'electrician', 'المهندسين، الجيزة', 30.0495, 31.1997],
    ['كريم فؤاد', '+201033334444', 'ac_technician', 'المعادي، القاهرة', 29.9660, 31.2500],
    ['مصطفى عمرو', '+201044445555', 'carpenter', 'شبرا الخيمة', 30.1220, 31.2720],
    ['رمضان سعيد', '+201055556666', 'plumber', 'الجيزة، الهرم', 29.9851, 31.1797],
    ['إيهاب شاكر', '+201066667777', 'electrician', 'الزمالك، القاهرة', 30.0583, 31.2161]
  ];

  technicians.forEach(([name, phone, specialty, location, lat, lng]) => {
    const email = `tech-${specialty}-${Date.now()}@example.com`;
    const userId = ensureUser(email, 'tech123', 'technician');
    const existing = db.prepare('SELECT id FROM technicians WHERE user_id = ?').get(userId);
    if (!existing) {
      db.prepare(
        `INSERT INTO technicians (user_id, name, phone, specialty, location_text, latitude, longitude, availability)
         VALUES (?, ?, ?, ?, ?, ?, ?, 'available')`
      ).run(userId, name, phone, specialty, location, lat, lng);
      console.log(`[seed] Added technician: ${name} (${specialty})`);
    }
  });

  // A sample pending request that gets assigned by the matching logic.
  const sampleCustomer = db.prepare('SELECT id FROM customers ORDER BY id LIMIT 1').get();
  if (sampleCustomer) {
    db.prepare(
      `INSERT INTO requests (customer_id, specialty, description, location_text, latitude, longitude)
       VALUES (?, 'plumber', 'تسريب ماء في حوض المطبخ', 'مدينة نصر، القاهرة', 30.0444, 31.2357)`
    ).run(sampleCustomer.id);
    const req = db.prepare("SELECT id FROM requests WHERE status = 'pending' ORDER BY id DESC LIMIT 1").get();
    if (req) {
      const tech = assignTechnician(req.id);
      console.log(`[seed] Sample request #${req.id} -> ${tech ? 'assigned to ' + tech.name : 'no technician available'}`);
    }
  }
}

seedAdmin();
if (process.env.SEED_SAMPLE === '1') seedSample();

console.log('[seed] Done.');
console.log('    Admin login : admin@fixrequest.com / admin123');