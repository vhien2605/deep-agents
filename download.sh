#!/usr/bin/env bash
set -euo pipefail

DATASET_DIR="${1:-dataset/CITYWALK}"
mkdir -p "$DATASET_DIR"
cd "$DATASET_DIR"

clone_at_commit() {
    local repo_url="$1"
    local directory="$2"
    local commit="$3"

    if [[ ! -d "$directory/.git" ]]; then
        git clone "$repo_url" "$directory"
    fi

    git -C "$directory" fetch --all --tags
    git -C "$directory" checkout "$commit"
}

clone_current() {
    local repo_url="$1"
    local directory="$2"

    if [[ ! -d "$directory/.git" ]]; then
        git clone "$repo_url" "$directory"
    else
        git -C "$directory" pull --ff-only
    fi
}

# Năm snapshot có commit chính xác trong artifact Zenodo
clone_at_commit \
    "https://github.com/hjson/hjson-cpp.git" \
    "hjson-cpp" \
    "0c40199c3f9de03068a8cf7191a3183c3f26a2ce"

clone_at_commit \
    "https://github.com/leethomason/tinyxml2.git" \
    "tinyxml2" \
    "1dee28e51f9175a31955b9791c74c430fe13dc82"

clone_at_commit \
    "https://github.com/jbeder/yaml-cpp.git" \
    "yaml-cpp" \
    "b591d8ae2ad1ff373273c3e05973adf6c46abfa8"

clone_at_commit \
    "https://github.com/google/re2.git" \
    "re2" \
    "bc423653fdf28618554da96e1532662d1e33eaca"

clone_at_commit \
    "https://github.com/jart/json.cpp.git" \
    "json.cpp" \
    "c39fbdac0f0f6638d5cfca43988750a1aac512db"

# Năm project có trong bài v2 nhưng không có snapshot trong artifact
clone_current \
    "https://github.com/ninja-build/ninja.git" \
    "ninja"

clone_current \
    "https://github.com/google/leveldb.git" \
    "leveldb"

clone_current \
    "https://github.com/colmap/glomap.git" \
    "glomap"

clone_current \
    "https://github.com/noahpop77/Papy.git" \
    "Papy"

clone_current \
    "https://github.com/ml-explore/mlx.git" \
    "mlx"

# Khởi tạo submodule nếu project có sử dụng
for project in \
    hjson-cpp tinyxml2 yaml-cpp re2 ninja \
    leveldb json.cpp glomap Papy mlx
do
    git -C "$project" submodule update --init --recursive || true
done

echo
echo "Downloaded CITYWALK projects:"
for project in \
    hjson-cpp tinyxml2 yaml-cpp re2 ninja \
    leveldb json.cpp glomap Papy mlx
do
    printf "%-15s %s\n" \
        "$project" \
        "$(git -C "$project" rev-parse HEAD)"
done