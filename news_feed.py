"""OptiBook WebSocket client - News Feed handling

This module provides functionality for handling news feed data from OptiBook.
It works in conjunction with the OptiBookTradeStream class.
"""

from datetime import datetime
from typing import Optional, Callable, Dict, List, Any
from collections import deque
import json

class NewsFeedHandler:
    def __init__(self, max_news_items: int = 1000):
        """
        Initialize the news feed handler.
        
        Args:
            max_news_items: Maximum number of news items to store in memory
        """
        self.all_news = deque(maxlen=max_news_items)
        self.on_news: Optional[Callable[[Dict], None]] = None
        self.store_news = True

    def process_news(self, data: Dict[str, Any]) -> None:
        """
        Process incoming news feed data.
        
        Args:
            data: Raw news feed data from the websocket
        """
        try:
            # Accept either plural or singular keys and both list/batched formats.
            posts = data.get("posts") if data.get("posts") is not None else data.get("post", [])
            timestamps = data.get("timestamps") if data.get("timestamps") is not None else data.get("timestamp", [])
            authors = data.get("authors") if data.get("authors") is not None else data.get("author", [])

            # If the server sent a single dict (not lists), normalize to lists
            if isinstance(posts, (str, dict)):
                posts = [posts]
            if isinstance(timestamps, (int, float, str)):
                timestamps = [timestamps]
            if isinstance(authors, (str, dict)):
                authors = [authors]

            # If no posts found but data contains top-level text, try to extract common fields
            if not posts and isinstance(data.get("text"), str):
                posts = [data.get("text")]

            # Iterate over available posts
            for i in range(len(posts)):
                raw_ts = None
                if i < len(timestamps):
                    raw_ts = timestamps[i]
                elif data.get("timestamp") and not isinstance(data.get("timestamp"), list):
                    raw_ts = data.get("timestamp")

                # Normalize timestamp to ISO-like string where possible
                if isinstance(raw_ts, (int, float)):
                    ts_val = datetime.utcfromtimestamp(float(raw_ts)).isoformat()
                else:
                    ts_val = str(raw_ts) if raw_ts is not None else datetime.utcnow().isoformat()

                news_item = {
                    "timestamp": ts_val,
                    "post": posts[i] if i < len(posts) else "",
                    "author": authors[i] if i < len(authors) else "unknown",
                    "sentiment_score": None,
                    "keywords": [],
                }

                # Store if enabled
                if self.store_news:
                    self.all_news.append(news_item)

                # Emit news item through callback
                if self.on_news:
                    try:
                        self.on_news(news_item)
                    except Exception as e:
                        print(f"Error in on_news callback: {e}")
                    
        except Exception as e:
            print(f"Error processing news: {e}")

    def get_recent_news(self, limit: Optional[int] = None) -> List[Dict]:
        """
        Get recent news items.
        
        Args:
            limit: Optional limit on number of items to return
            
        Returns:
            List of news items, most recent first
        """
        news = list(self.all_news)
        if limit:
            return news[-limit:]
        return news

    def save_news_to_file(self, filename: str, format: str = 'json') -> None:
        """
        Save collected news items to a file.
        
        Args:
            filename: Name of the file to save to
            format: File format ('json' or 'csv')
        """
        news = list(self.all_news)
        if not news:
            print("No news items to save")
            return

        try:
            if format.lower() == 'json':
                with open(filename, 'w') as f:
                    json.dump(news, f, indent=2)
            elif format.lower() == 'csv':
                import csv
                with open(filename, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=news[0].keys())
                    writer.writeheader()
                    writer.writerows(news)
            print(f"Saved {len(news)} news items to {filename}")
        except Exception as e:
            print(f"Error saving news items: {e}")

    def analyze_sentiment(self, text: str) -> float:
        """
        Basic sentiment analysis of news text.
        You can implement more sophisticated sentiment analysis here.
        
        Args:
            text: The news text to analyze
            
        Returns:
            sentiment score between -1 (negative) and 1 (positive)
        """
        # This is a placeholder - you should implement proper sentiment analysis
        # You might want to use NLTK, TextBlob, or a more sophisticated model
        positive_words = {'up', 'increase', 'gain', 'positive', 'profit', 'success'}
        negative_words = {'down', 'decrease', 'loss', 'negative', 'fail', 'risk'}
        
        words = text.lower().split()
        pos_count = sum(1 for word in words if word in positive_words)
        neg_count = sum(1 for word in words if word in negative_words)
        
        if pos_count + neg_count == 0:
            return 0.0
            
        return (pos_count - neg_count) / (pos_count + neg_count)

    def extract_keywords(self, text: str, num_keywords: int = 5) -> List[str]:
        """
        Extract key terms from news text.
        You can implement more sophisticated keyword extraction here.
        
        Args:
            text: The news text to analyze
            num_keywords: Number of keywords to extract
            
        Returns:
            List of keywords
        """
        # This is a placeholder - you should implement proper keyword extraction
        # You might want to use NLTK, spaCy, or other NLP libraries
        words = text.lower().split()
        stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to'}
        keywords = [word for word in words if word not in stopwords]
        return keywords[:num_keywords]

def example_news_handler(news_item: Dict):
    """Example callback function for handling news items."""
    print(f"[{news_item['timestamp']}] {news_item['author']}: {news_item['post']}")

# Example usage
if __name__ == "__main__":
    # Create news handler
    news_handler = NewsFeedHandler()
    
    # Set up callback
    news_handler.on_news = example_news_handler
    
    # Example news data
    example_data = {
        "posts": ["Market showing strong upward trend", "New regulations announced"],
        "timestamps": ["2025-11-01T10:00:00", "2025-11-01T10:01:00"],
        "authors": ["analyst1", "news_bot"]
    }
    
    # Process example news
    news_handler.process_news(example_data)
    
    # Get recent news
    recent_news = news_handler.get_recent_news(limit=5)
    print("\nRecent news items:")
    for item in recent_news:
        print(f"{item['timestamp']}: {item['post']}")
    
    # Save to file
    news_handler.save_news_to_file("news_feed.json")