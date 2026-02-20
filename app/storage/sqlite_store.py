# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
SQLite Storage for Taey-Ed V7.
Zero external dependencies, py2app-friendly.
Stores extracted content for Phase 6 question answering.
Exportable to DeepTutor JSON format.
"""

import sqlite3
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

# Database directory - in user's home directory
DB_DIR = Path.home() / "deeptutor_data"


def _make_db_path(platform: str, course_id: str) -> Path:
    """Build per-course DB path: ~/deeptutor_data/{platform}_{course_id}.db"""
    safe_name = f"{platform}_{course_id}".replace("/", "_").replace(" ", "_")
    return DB_DIR / f"{safe_name}.db"


class TaeyEdStorage:
    """
    SQLite storage for extracted educational content.
    One DB per platform+course. Zero dependencies (sqlite3 is stdlib).
    """

    def __init__(self, platform: str = None, course_id: str = None, db_path: Path = None):
        """Initialize storage. Pass platform + course_id for per-course DB."""
        if db_path:
            self.db_path = db_path
        elif platform and course_id:
            self.db_path = _make_db_path(platform, course_id)
        else:
            raise ValueError("TaeyEdStorage requires platform + course_id (or explicit db_path)")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = None
        self._initialize_db()

    def _initialize_db(self):
        """Create database and tables if they don't exist."""
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row  # Enable dict-like access
        cursor = self.conn.cursor()

        # Enable foreign keys
        cursor.execute('PRAGMA foreign_keys = ON')

        # Courses table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS courses (
                course_id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                course_name TEXT NOT NULL,
                subject TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, course_name)
            )
        ''')

        # Extracted content table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS content (
                content_id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER,
                screen_type TEXT NOT NULL,
                lesson TEXT,
                texts TEXT,
                images TEXT,
                embeddings TEXT,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (course_id) REFERENCES courses(course_id)
            )
        ''')

        # Q&A pairs table (for answered questions)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS qa_pairs (
                qa_id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                question_type TEXT,
                correct BOOLEAN,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (course_id) REFERENCES courses(course_id)
            )
        ''')

        # Checkpoints table (P1.2: resume after crash/restart)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                course_id TEXT NOT NULL,
                app_name TEXT NOT NULL,
                screens_completed INTEGER DEFAULT 0,
                last_screen TEXT,
                last_action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, course_id, app_name)
            )
        ''')

        # Create indexes for faster queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_content_course
            ON content(course_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_qa_course
            ON qa_pairs(course_id)
        ''')

        self.conn.commit()
        logger.info(f"Initialized SQLite database at {self.db_path}")

    def get_or_create_course(self, platform: str, course_name: str) -> int:
        """Get existing course or create new one. Returns course_id."""
        cursor = self.conn.cursor()

        # Try to find existing
        cursor.execute('''
            SELECT course_id FROM courses
            WHERE platform = ? AND course_name = ?
        ''', (platform, course_name))
        row = cursor.fetchone()

        if row:
            return row['course_id']

        # Create new
        cursor.execute('''
            INSERT INTO courses (platform, course_name)
            VALUES (?, ?)
        ''', (platform, course_name))
        self.conn.commit()
        return cursor.lastrowid

    def store_content(
        self,
        platform: str,
        course_id: str,
        screen_type: str,
        texts: List[str],
        images: List[Dict],
        embeddings: List[float] = None,
        lesson: str = ""
    ) -> int:
        """
        Store extracted content.

        Args:
            platform: Platform name (e.g., "acellus")
            course_id: Course identifier
            screen_type: Screen type from YAML
            texts: Extracted text content
            images: Image descriptions from VLM
            embeddings: Optional embedding vector
            lesson: Lesson name

        Returns:
            content_id of stored record
        """
        db_course_id = self.get_or_create_course(platform, course_id)
        cursor = self.conn.cursor()

        cursor.execute('''
            INSERT INTO content
            (course_id, screen_type, lesson, texts, images, embeddings)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            db_course_id,
            screen_type,
            lesson,
            json.dumps(texts),
            json.dumps(images),
            json.dumps(embeddings) if embeddings else None
        ))
        self.conn.commit()

        content_id = cursor.lastrowid
        logger.info(f"Stored content {content_id} for {platform}/{course_id}")
        return content_id

    def store_qa_pair(
        self,
        platform: str,
        course_id: str,
        question: str,
        answer: str,
        question_type: str = None,
        correct: bool = None
    ) -> int:
        """Store a question-answer pair."""
        db_course_id = self.get_or_create_course(platform, course_id)
        cursor = self.conn.cursor()

        cursor.execute('''
            INSERT INTO qa_pairs
            (course_id, question, answer, question_type, correct)
            VALUES (?, ?, ?, ?, ?)
        ''', (db_course_id, question, answer, question_type, correct))
        self.conn.commit()

        return cursor.lastrowid

    def get_recent_content(
        self,
        platform: str,
        course_id: str,
        limit: int = 10
    ) -> List[Dict]:
        """
        Get recent extracted content for a course.
        Used for Phase 6 question answering context.
        """
        db_course_id = self.get_or_create_course(platform, course_id)
        cursor = self.conn.cursor()

        cursor.execute('''
            SELECT screen_type, lesson, texts, images, extracted_at
            FROM content
            WHERE course_id = ?
            ORDER BY extracted_at DESC
            LIMIT ?
        ''', (db_course_id, limit))

        results = []
        for row in cursor.fetchall():
            results.append({
                'screen_type': row['screen_type'],
                'lesson': row['lesson'],
                'texts': json.loads(row['texts']) if row['texts'] else [],
                'images': json.loads(row['images']) if row['images'] else [],
                'extracted_at': row['extracted_at']
            })
        return results

    def search_content(
        self,
        platform: str,
        course_id: str,
        query: str,
        limit: int = 5
    ) -> List[Dict]:
        """
        Simple text search in extracted content.
        For Phase 6: find relevant content for answering questions.
        """
        db_course_id = self.get_or_create_course(platform, course_id)
        cursor = self.conn.cursor()

        # Simple LIKE search (can upgrade to FTS5 later)
        cursor.execute('''
            SELECT screen_type, lesson, texts, images
            FROM content
            WHERE course_id = ? AND texts LIKE ?
            ORDER BY extracted_at DESC
            LIMIT ?
        ''', (db_course_id, f'%{query}%', limit))

        results = []
        for row in cursor.fetchall():
            results.append({
                'screen_type': row['screen_type'],
                'lesson': row['lesson'],
                'texts': json.loads(row['texts']) if row['texts'] else [],
                'images': json.loads(row['images']) if row['images'] else []
            })
        return results

    def get_stats(self, platform: str = None) -> Dict[str, Any]:
        """Get storage statistics."""
        cursor = self.conn.cursor()

        if platform:
            cursor.execute('''
                SELECT COUNT(*) as content_count FROM content c
                JOIN courses co ON c.course_id = co.course_id
                WHERE co.platform = ?
            ''', (platform,))
        else:
            cursor.execute('SELECT COUNT(*) as content_count FROM content')

        content_count = cursor.fetchone()['content_count']

        cursor.execute('SELECT COUNT(*) as course_count FROM courses')
        course_count = cursor.fetchone()['course_count']

        cursor.execute('SELECT COUNT(*) as qa_count FROM qa_pairs')
        qa_count = cursor.fetchone()['qa_count']

        return {
            'content_records': content_count,
            'courses': course_count,
            'qa_pairs': qa_count,
            'db_path': str(self.db_path)
        }

    def export_for_deeptutor(self, platform: str, course_id: str, output_file: str) -> str:
        """
        Export course data in DeepTutor-compatible JSON format.

        Args:
            platform: Platform name
            course_id: Course identifier
            output_file: Output JSON file path

        Returns:
            Path to exported file
        """
        db_course_id = self.get_or_create_course(platform, course_id)
        cursor = self.conn.cursor()

        # Get all content
        cursor.execute('''
            SELECT screen_type, lesson, texts, images, extracted_at
            FROM content
            WHERE course_id = ?
            ORDER BY extracted_at
        ''', (db_course_id,))
        content_rows = cursor.fetchall()

        # Get all Q&A pairs
        cursor.execute('''
            SELECT question, answer, question_type, correct
            FROM qa_pairs
            WHERE course_id = ?
        ''', (db_course_id,))
        qa_rows = cursor.fetchall()

        # Build export structure
        export_data = {
            "metadata": {
                "source": "Taey-Ed V7",
                "exported_at": datetime.now().isoformat(),
                "platform": platform,
                "course_id": course_id
            },
            "documents": [],
            "qa_pairs": []
        }

        # Add content documents
        for row in content_rows:
            texts = json.loads(row['texts']) if row['texts'] else []
            images = json.loads(row['images']) if row['images'] else []

            doc = {
                "content": "\n".join(texts),
                "metadata": {
                    "screen_type": row['screen_type'],
                    "lesson": row['lesson'],
                    "extracted_at": row['extracted_at'],
                    "image_descriptions": [img.get('description', '') for img in images]
                }
            }
            export_data["documents"].append(doc)

        # Add Q&A pairs
        for row in qa_rows:
            qa = {
                "question": row['question'],
                "answer": row['answer'],
                "question_type": row['question_type'],
                "correct": row['correct']
            }
            export_data["qa_pairs"].append(qa)

        # Write to file
        output_path = Path(output_file)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Exported {len(export_data['documents'])} docs, {len(export_data['qa_pairs'])} Q&A to {output_file}")
        return str(output_path)

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()


# Module-level cache: (platform, course_id) -> TaeyEdStorage
_storage_cache: dict[tuple[str, str], TaeyEdStorage] = {}


def get_storage(platform: str, course_id: str) -> TaeyEdStorage:
    """Get or create storage for a specific platform + course."""
    key = (platform, course_id)
    if key not in _storage_cache:
        _storage_cache[key] = TaeyEdStorage(platform=platform, course_id=course_id)
    return _storage_cache[key]


if __name__ == "__main__":
    # Test storage
    storage = TaeyEdStorage(platform="acellus", course_id="intro_banking")

    # Store some test content
    content_id = storage.store_content(
        platform="acellus",
        course_id="intro_banking",
        screen_type="VIDEO_LESSON",
        texts=["Banks hold deposits.", "Banks make loans."],
        images=[{"description": "Money flow diagram", "purpose": "diagram"}],
        lesson="What is a Bank"
    )
    print(f"Stored content: {content_id}")

    # Get stats
    stats = storage.get_stats()
    print(f"Stats: {stats}")

    # Search
    results = storage.search_content("acellus", "intro_banking", "deposits")
    print(f"Search results: {len(results)}")

    storage.close()
