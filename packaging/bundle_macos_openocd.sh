#!/bin/zsh
set -euo pipefail

SOURCE="${1:?openocd executable required}"
DEST="${2:?destination required}"
prefix="${SOURCE:h:h}"
source_lib_dir="$prefix/lib"
mkdir -p "$DEST/bin" "$DEST/lib" "$DEST/share/openocd"
cp "$SOURCE" "$DEST/bin/openocd"

typeset -a queue
queue=("$DEST/bin/openocd")
while (( ${#queue[@]} )); do
  current="${queue[1]}"
  queue[1]=()
  while IFS= read -r dependency; do
    case "$dependency" in
      /opt/homebrew/*|/usr/local/*)
        source_dependency="$dependency"
        ;;
      @executable_path/../lib/*|@loader_path/*)
        source_dependency="$source_lib_dir/${dependency:t}"
        [[ -f "$source_dependency" ]] || {
          echo "Missing bundled OpenOCD dependency: $source_dependency" >&2
          exit 1
        }
        ;;
      *)
        continue
        ;;
    esac
    name="${dependency:t}"
    target="$DEST/lib/$name"
    if [[ ! -e "$target" ]]; then
      cp -L "$source_dependency" "$target"
      chmod u+w "$target"
      queue+=("$target")
    fi
    if [[ "$current" == "$DEST/bin/openocd" ]]; then
      replacement="@executable_path/../lib/$name"
    else
      replacement="@loader_path/$name"
    fi
    install_name_tool -change "$dependency" "$replacement" "$current"
  done < <(otool -L "$current" | tail -n +2 | awk '{print $1}')
  if [[ "$current" == "$DEST/lib/"* ]]; then
    install_name_tool -id "@loader_path/${current:t}" "$current"
  fi
done

scripts="$prefix/share/openocd/scripts"
[[ -d "$scripts" ]] || { echo "Missing OpenOCD scripts: $scripts" >&2; exit 1; }
ditto "$scripts" "$DEST/share/openocd/scripts"
for library in "$DEST/lib/"*.dylib(N); do codesign --force --sign - "$library"; done
codesign --force --sign - "$DEST/bin/openocd"
