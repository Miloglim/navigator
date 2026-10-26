import unittest
from unittest.mock import patch

import manual_server


class ImageEntryTests(unittest.TestCase):
    def test_same_resolved_file_is_rendered_once(self):
        row = {
            "图片名": "@media.png",
            "消息原文": "price [图片消息](mediaId=@media)",
        }
        with patch.object(manual_server, "_resolve_image",
                          return_value=("/images/%40media.png", "@media.png")):
            images = manual_server.images_for(row)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["file"], "@media.png")

    def test_distinct_files_are_preserved(self):
        row = {"图片名": "@one.png,@two.png", "消息原文": ""}
        mapping = {"@one.png": ("/images/%40one.png", "@one.png"),
                   "@two.png": ("/images/%40two.png", "@two.png")}
        with patch.object(manual_server, "_resolve_image", side_effect=lambda name: mapping[name]):
            images = manual_server.images_for(row)
        self.assertEqual([row["file"] for row in images], ["@one.png", "@two.png"])


if __name__ == "__main__":
    unittest.main()
