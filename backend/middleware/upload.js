/**
 * @file   Multer upload middleware with type validation
 * @module upload
 *
 * Handles the multipart endpoints: size and file-count limits, magic-byte type detection,
 * server-generated filenames and extensions, and cleanup of rejected files.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Multipart upload middleware for technician documents.
// Files land in an isolated 'pending' staging folder, then are moved to a
// per-technician folder inside the registration transaction.
const path = require('path');
const fs = require('fs');
const multer = require('multer');
const FileType = require('file-type');

const UPLOAD_ROOT = path.join(__dirname, '..', '..', 'uploads', 'technicians');

// Per-file ceiling from the environment (MAX_UPLOAD_MB, default 5). The
// profile image keeps its own tighter 3 MB default.
const MAX_MB = Number(process.env.MAX_UPLOAD_MB) > 0 ? Number(process.env.MAX_UPLOAD_MB) : 5;
const DOC_BYTES = Math.min(MAX_MB, 5) * 1024 * 1024;
const PROFILE_BYTES = Math.min(MAX_MB, 3) * 1024 * 1024;
fs.mkdirSync(path.join(UPLOAD_ROOT, 'pending'), { recursive: true });

// --- Content-based (magic bytes) validation -------------------------------
// The multer fileFilters below can only trust the client-supplied Content-Type,
// which is trivially spoofed: a PHP shell named photo.jpg sent as image/jpeg
// passes them. Everything below therefore re-checks the file's real signature.

// The extension we are willing to leave on disk for each allowed type. The
// multer filename generators take the extension from the client-supplied
// originalname, so a stored file can be named `...php` / `...phtml` / `...jsp`.
// After validation we rewrite the extension from the DETECTED type, so the
// on-disk name can never carry an executable extension.
const ALLOWED_EXT = {
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
  'application/pdf': '.pdf',
};

/**
 * Validates a file by reading its magic bytes (file signature).
 * This runs AFTER multer has written the file to disk (in the pending
 * folder), so we read from the saved path, not from a stream.
 *
 * @param {string} filePath  Absolute path to the uploaded file
 * @param {string[]} allowedMimes  e.g. ['image/jpeg', 'image/png']
 * @returns {Promise<{ok: boolean, detected: string|null, error?: string}>}
 */
async function validateMagicBytes(filePath, allowedMimes) {
  try {
    const detected = await FileType.fromFile(filePath);

    if (!detected) {
      return { ok: false, detected: null, error: 'تعذر التعرف على نوع الملف' };
    }

    if (!allowedMimes.includes(detected.mime)) {
      return {
        ok: false,
        detected: detected.mime,
        error: 'نوع الملف الفعلي غير مسموح (' + detected.mime + ')',
      };
    }

    return { ok: true, detected: detected.mime };
  } catch (err) {
    return { ok: false, detected: null, error: 'خطأ في قراءة الملف: ' + err.message };
  }
}

// Renames an accepted file so its extension matches the type we actually
// detected, discarding whatever extension the client asked for.
function normalizeExtension(file, detectedMime) {
  const wanted = ALLOWED_EXT[detectedMime];
  if (!wanted) return file.path;

  const current = path.extname(file.path).toLowerCase();
  if (current === wanted) return file.path;

  const target = path.join(
    path.dirname(file.path),
    path.basename(file.path, path.extname(file.path)) + wanted
  );
  try {
    fs.renameSync(file.path, target);
    file.path = target;
    return target;
  } catch (err) {
    // If the rename fails the upload is still rejected rather than accepted
    // with an attacker-chosen extension.
    const e = new Error('تعذر توحيد امتداد الملف');
    e.status = 400;
    throw e;
  }
}

/**
 * Runs after multer. Validates every uploaded file's magic bytes.
 * Deletes offending files and returns 400 if any file fails.
 *
 * Allowed mimes:
 *   - technician documents (criminal_record, national_id):
 *     image/jpeg, image/png, image/webp, application/pdf
 *   - profile_image: image/jpeg, image/png, image/webp
 *   - portfolio_images: image/jpeg, image/png, image/webp
 */
function magicBytesGuard(options) {
  options = options || {};
  const allowed = options.allowedMimes || ['image/jpeg', 'image/png', 'image/webp'];

  return async function (req, res, next) {
    const filesToCheck = [];

    // Handle multer's different shapes: req.file (single), req.files (array or map)
    if (req.file) {
      filesToCheck.push(req.file);
    } else if (Array.isArray(req.files)) {
      filesToCheck.push(...req.files);
    } else if (req.files && typeof req.files === 'object') {
      Object.values(req.files).forEach((arr) => {
        if (Array.isArray(arr)) filesToCheck.push(...arr);
        else if (arr) filesToCheck.push(arr);
      });
    }

    if (filesToCheck.length === 0) {
      return next(); // no files to check -> not a file-upload request
    }

    // Deletes every file this request produced, so a rejected upload leaves
    // nothing behind in the pending folder.
    const cleanup = () => {
      filesToCheck.forEach((f) => {
        try {
          fs.unlinkSync(f.path);
        } catch (e) {
          /* already gone */
        }
      });
    };

    for (const file of filesToCheck) {
      const result = await validateMagicBytes(file.path, allowed);
      if (!result.ok) {
        cleanup();
        return res.status(400).json({
          error: 'الملف المرفوع غير صالح: ' + (result.error || 'نوع غير معروف'),
          detected: result.detected,
        });
      }
      // Remember the accepted type so the extension rewrite below uses exactly
      // the verdict that was just made, with no second read of the file.
      file.detectedMime = result.detected;
    }

    // All files are genuine. Now drop any client-controlled extension.
    try {
      filesToCheck.forEach((f) => normalizeExtension(f, f.detectedMime));
    } catch (err) {
      cleanup();
      return res.status(400).json({ error: err.message, detected: null });
    }

    return next();
  };
}

const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    const tmp = path.join(UPLOAD_ROOT, 'pending');
    fs.mkdirSync(tmp, { recursive: true });
    cb(null, tmp);
  },
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname || '').toLowerCase() || '.jpg';
    const name = file.fieldname + '-' + Date.now() + '-' + Math.round(Math.random() * 1e6) + ext;
    cb(null, name);
  },
});

function fileFilter(req, file, cb) {
  const allowed = ['image/jpeg', 'image/png', 'image/webp', 'application/pdf'];
  if (!allowed.includes(file.mimetype)) {
    return cb(new Error('صيغة الملف غير مدعومة. المسموح: JPG, PNG, WEBP, PDF'));
  }
  cb(null, true);
}

const technicianDocsUpload = multer({
  storage,
  fileFilter,
  limits: { fileSize: DOC_BYTES, files: 2 },
}).fields([
  { name: 'criminal_record', maxCount: 1 },
  { name: 'national_id', maxCount: 1 },
]);

// --- Profile image + portfolio uploads (Phase 1: profiles and reviews) ---
// Like the document uploads these land in a 'pending' staging folder and are
// moved into a per-technician folder by the route handler, so the DB path is
// only written once the file is in its final home.
const profileStorage = multer.diskStorage({
  destination: (req, file, cb) => {
    const dir = path.join(UPLOAD_ROOT, 'profiles', 'pending');
    fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname || '').toLowerCase() || '.jpg';
    const name = 'profile-' + Date.now() + '-' + Math.round(Math.random() * 1e6) + ext;
    cb(null, name);
  },
});

const portfolioStorage = multer.diskStorage({
  destination: (req, file, cb) => {
    const dir = path.join(UPLOAD_ROOT, 'portfolio', 'pending');
    fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname || '').toLowerCase() || '.jpg';
    const name = 'work-' + Date.now() + '-' + Math.round(Math.random() * 1e6) + ext;
    cb(null, name);
  },
});

function imageFilter(req, file, cb) {
  const allowed = ['image/jpeg', 'image/png', 'image/webp'];
  if (!allowed.includes(file.mimetype)) {
    // Multer forwards this raw Error to the error handler, so the status has to
    // be set explicitly; otherwise a bad upload surfaces as a 500.
    const err = new Error('صيغة الصورة غير مدعومة');
    err.status = 400;
    return cb(err);
  }
  cb(null, true);
}

const profileImageUpload = multer({
  storage: profileStorage,
  fileFilter: imageFilter,
  limits: { fileSize: PROFILE_BYTES, files: 1 },
}).single('profile_image');

const portfolioUpload = multer({
  storage: portfolioStorage,
  fileFilter: imageFilter,
  limits: { fileSize: DOC_BYTES, files: 5 },
}).array('portfolio_images', 5);

module.exports = {
  technicianDocsUpload,
  profileImageUpload,
  portfolioUpload,
  magicBytesGuard,
  validateMagicBytes,
  UPLOAD_ROOT,
};
