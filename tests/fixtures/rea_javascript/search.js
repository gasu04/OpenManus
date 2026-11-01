function searchItems(items, query) {
  return items.filter((item) => item.includes(query));
}

module.exports = { searchItems };
