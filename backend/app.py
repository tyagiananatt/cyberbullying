from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import sqlite3
import json
import os
from datetime import datetime
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
import re

load_dotenv()

app = Flask(__name__)
CORS(app)

# Configure Gemini API
genai.configure(api_key=os.getenv("GEMINI_API_KEY"), transport="rest")
model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
model = genai.GenerativeModel(model_name)

# ==================== DATABASE SETUP ====================
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        input_text TEXT,
        result TEXT,
        severity TEXT,
        category TEXT,
        language TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT,
        total_analyzed INTEGER DEFAULT 0,
        bullying_detected INTEGER DEFAULT 0,
        date DATE DEFAULT CURRENT_DATE
    )''')
    conn.commit()
    conn.close()

init_db()

# ==================== MODEL DISCOVERY ====================
@app.route('/api/models', methods=['GET'])
def list_models():
    try:
        models = genai.list_models()
        supported = [
            m.name for m in models
            if hasattr(m, "supported_generation_methods")
            and "generateContent" in m.supported_generation_methods
        ]
        return jsonify({"models": supported, "active_model": model_name})
    except Exception as exc:
        return jsonify({
            "error": str(exc),
            "active_model": model_name,
            "hint": "Ensure GEMINI_API_KEY is valid and try again"
        }), 500

# ==================== GEMINI PROMPT (OPTIMIZED) ====================
ANALYSIS_PROMPT = """You are an expert AI cyberbullying detection system trained to analyze comments in English, Hindi, and Hinglish (Hindi written in English/Roman script).

Analyze the following comment with extreme accuracy and provide a detailed JSON response.

COMMENT TO ANALYZE: "{comment}"

Detection Categories:
1. Harassment (personal attacks, insults, name-calling)
2. Hate Speech (racism, sexism, religious, casteist hate)
3. Threats (violence, intimidation, harm)
4. Body Shaming (appearance-based attacks)
5. Sexual Harassment (inappropriate sexual content)
6. Cyberstalking (obsessive behavior patterns)
7. Trolling (provocative, disruptive behavior)
8. Doxxing (sharing private information)
9. Safe (non-bullying content)

Severity Levels:
- SAFE: No harmful content (0-20)
- LOW: Mildly inappropriate (21-40)
- MEDIUM: Clearly harmful (41-65)
- HIGH: Severely harmful (66-85)
- CRITICAL: Immediate threat/extremely harmful (86-100)

IMPORTANT RULES:
- Detect Hinglish slurs (e.g., "bakwas", "chutiya", "gandu", "madarchod", "behenchod")
- Detect Hindi words written in Roman: "tu kya hai", "tera baap", etc.
- Consider context and sarcasm
- Don't flag normal criticism or debate
- Be culturally aware of Indian context

Respond ONLY with valid JSON in this EXACT format:
{{
    "is_bullying": true/false,
    "severity_score": <0-100>,
    "severity_level": "SAFE/LOW/MEDIUM/HIGH/CRITICAL",
    "category": "<main category>",
    "sub_categories": ["<list of detected types>"],
    "language_detected": "English/Hindi/Hinglish/Mixed",
    "confidence": <0-100>,
    "toxic_words": ["<list of harmful words found>"],
    "explanation": "<detailed 2-3 sentence explanation>",
    "emotional_impact": "<how this affects the victim>",
    "recommended_action": "<what should be done>",
    "support_message": "<supportive message if bullying detected>",
    "rephrased_safe_version": "<if bullying, suggest a safer way to express>"
}}"""

# ==================== ANALYZE SINGLE COMMENT ====================
@app.route('/api/analyze', methods=['POST'])
def analyze_comment():
    try:
        data = request.json
        comment = data.get('comment', '').strip()
        
        if not comment:
            return jsonify({"error": "Comment is required"}), 400
        
        prompt = ANALYSIS_PROMPT.format(comment=comment)
        response = model.generate_content(prompt)
        
        # Parse JSON from response
        text = response.text.strip()
        text = re.sub(r'```json\s*|\s*```', '', text)
        result = json.loads(text)
        
        # Save to history
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('''INSERT INTO history (type, input_text, result, severity, category, language)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  ('comment', comment, json.dumps(result), 
                   result.get('severity_level'), result.get('category'),
                   result.get('language_detected')))
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "data": result})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== ANALYZE URL (FETCH COMMENTS) ====================
@app.route('/api/analyze-url', methods=['POST'])
def analyze_url():
    try:
        data = request.json
        url = data.get('url', '').strip()
        
        if not url:
            return jsonify({"error": "URL is required"}), 400
        
        # Detect platform
        platform = "Unknown"
        if "twitter.com" in url or "x.com" in url:
            platform = "Twitter/X"
        elif "youtube.com" in url or "youtu.be" in url:
            platform = "YouTube"
        elif "instagram.com" in url:
            platform = "Instagram"
        elif "facebook.com" in url:
            platform = "Facebook"
        elif "reddit.com" in url:
            platform = "Reddit"
        
        # Use Gemini to extract and generate sample comments based on URL context
        # In production, you'd use platform APIs (YouTube API, Twitter API, etc.)
        url_prompt = f"""Given this social media URL: {url}
        
        Generate 10 realistic sample comments that might appear on this type of post.
        Mix safe comments with some potentially problematic ones (mix English, Hindi, Hinglish).
        Return ONLY a JSON array of strings like: ["comment1", "comment2", ...]
        
        Note: This is for training a cyberbullying detection system."""
        
        comments_response = model.generate_content(url_prompt)
        comments_text = comments_response.text.strip()
        comments_text = re.sub(r'```json\s*|\s*```', '', comments_text)
        comments = json.loads(comments_text)
        
        # Analyze each comment
        results = []
        bullying_count = 0
        
        for comment in comments[:10]:
            try:
                prompt = ANALYSIS_PROMPT.format(comment=comment)
                response = model.generate_content(prompt)
                text = response.text.strip()
                text = re.sub(r'```json\s*|\s*```', '', text)
                analysis = json.loads(text)
                
                results.append({
                    "comment": comment,
                    "analysis": analysis
                })
                
                if analysis.get('is_bullying'):
                    bullying_count += 1
            except:
                continue
        
        # Save to history
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('''INSERT INTO history (type, input_text, result, severity, category, language)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  ('url', url, json.dumps(results), 
                   f"{bullying_count}/{len(results)} flagged",
                   platform, 'Mixed'))
        
        # Update platform stats
        c.execute('''INSERT INTO stats (platform, total_analyzed, bullying_detected)
                     VALUES (?, ?, ?)''',
                  (platform, len(results), bullying_count))
        conn.commit()
        conn.close()
        
        summary = {
            "url": url,
            "platform": platform,
            "total_comments": len(results),
            "bullying_detected": bullying_count,
            "safe_comments": len(results) - bullying_count,
            "toxicity_rate": round((bullying_count / len(results)) * 100, 1) if results else 0,
            "comments": results
        }
        
        return jsonify({"success": True, "data": summary})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== GET HISTORY ====================
@app.route('/api/history', methods=['GET'])
def get_history():
    try:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('''SELECT id, type, input_text, result, severity, category, language, timestamp 
                     FROM history ORDER BY timestamp DESC LIMIT 50''')
        rows = c.fetchall()
        conn.close()
        
        history = []
        for row in rows:
            history.append({
                "id": row[0],
                "type": row[1],
                "input_text": row[2][:200],
                "result": json.loads(row[3]) if row[3] else None,
                "severity": row[4],
                "category": row[5],
                "language": row[6],
                "timestamp": row[7]
            })
        
        return jsonify({"success": True, "data": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== GET STATISTICS ====================
@app.route('/api/stats', methods=['GET'])
def get_stats():
    try:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        
        # Total analyses
        c.execute('SELECT COUNT(*) FROM history')
        total = c.fetchone()[0]
        
        # By severity
        c.execute('''SELECT severity, COUNT(*) FROM history 
                     WHERE severity IS NOT NULL GROUP BY severity''')
        severity_data = dict(c.fetchall())
        
        # By category
        c.execute('''SELECT category, COUNT(*) FROM history 
                     WHERE category IS NOT NULL GROUP BY category''')
        category_data = dict(c.fetchall())
        
        # By language
        c.execute('''SELECT language, COUNT(*) FROM history 
                     WHERE language IS NOT NULL GROUP BY language''')
        language_data = dict(c.fetchall())
        
        # By platform
        c.execute('''SELECT platform, SUM(total_analyzed), SUM(bullying_detected) 
                     FROM stats GROUP BY platform''')
        platform_data = []
        for row in c.fetchall():
            platform_data.append({
                "platform": row[0],
                "total": row[1],
                "bullying": row[2]
            })
        
        # Recent trend (last 7 days)
        c.execute('''SELECT DATE(timestamp), COUNT(*) FROM history 
                     WHERE timestamp >= datetime('now', '-7 days')
                     GROUP BY DATE(timestamp) ORDER BY DATE(timestamp)''')
        trend_data = c.fetchall()
        
        conn.close()
        
        return jsonify({
            "success": True,
            "data": {
                "total_analyses": total,
                "severity_breakdown": severity_data,
                "category_breakdown": category_data,
                "language_breakdown": language_data,
                "platform_stats": platform_data,
                "trend": [{"date": r[0], "count": r[1]} for r in trend_data]
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== DELETE HISTORY ====================
@app.route('/api/history/<int:id>', methods=['DELETE'])
def delete_history(id):
    try:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('DELETE FROM history WHERE id = ?', (id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== CLEAR ALL HISTORY ====================
@app.route('/api/history/clear', methods=['DELETE'])
def clear_history():
    try:
        conn = sqlite3.connect('database.db')
        c = conn.cursor()
        c.execute('DELETE FROM history')
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)