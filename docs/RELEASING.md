# リリース手順

1. `miyamizu/__init__.py` の `__version__` を新しい番号にする(例: `1.0.2`)。README の「バージョン」も合わせる。
2. `CHANGELOG.md` の先頭に `## 1.0.2` の節を追加する(Release の本文にそのまま使われます)。
3. コミットして push する。
4. タグを付けて push する。
   ```
   git tag v1.0.2
   git push origin v1.0.2
   ```
5. GitHub の Actions タブで `release` が緑になるのを待つ(テスト → Linux/Windows ビルド → 下書き Release 作成)。
6. Releases に **Draft** が出来ているので、本文と添付(`Miyamizu-linux-x86_64.zip` / `Miyamizu-windows-x64.zip` / `SHA256SUMS.txt`)を確認して **Publish release**。

注意:
- タグ名(`v1.0.2`)と `__version__`(`1.0.2`)が違うと最初のジョブで止まります。タグを打ち直す場合: `git tag -d v1.0.2 && git push origin :refs/tags/v1.0.2`。
- `CHANGELOG.md` に該当バージョンの節が無いと止まります。
- Actions タブの `Run workflow`(手動実行)ではビルドだけ行い、Release は作りません(成果物は Artifacts から取得)。
- 古い `.github/workflows/build-windows.yml` は `release.yml` に統合したので削除してください。
