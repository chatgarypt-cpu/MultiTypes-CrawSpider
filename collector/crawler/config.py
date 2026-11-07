"""Paths, constants and the shared HTTP user agent."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # collector/
DATA = os.path.join(ROOT, "data")
DB = os.path.join(DATA, "state.db")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MEDIA_COLS = ["id","title","url","link","mime","width","height","alt","caption",
              "description","created_at","parent","host"]

COLS = ["title","sub_title","url","channel_text","country_text","lang_text","user_id","user_author_name","user_url",
        "created_at","description","text","image","video","link","view_count","like_count","reply_count",
        "retweet_count","dislike_count","favourite_count","tag","keyword","host","first_channel","second_channel",
        "third_channel","editor","updated_at","hit_num","read_num"]

WORKERS = 8           # set by the CLI, used by the wp-api channel
