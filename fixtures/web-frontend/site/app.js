(function () {
  var list = document.getElementById("product-list");
  var loading = document.getElementById("loading");
  var form = document.getElementById("signup");
  var status = document.getElementById("signup-status");

  function render(products) {
    list.textContent = "";
    products.forEach(function (product) {
      var item = document.createElement("li");
      var name = document.createElement("span");
      var price = document.createElement("span");
      name.textContent = product.name;
      price.textContent = "$" + product.price;
      item.appendChild(name);
      item.appendChild(price);
      list.appendChild(item);
    });
  }

  function load() {
    loading.classList.add("is-visible");
    fetch("/api/products").then(function (response) {
      if (!response.ok) {
        return;
      }
      return response.json().then(function (products) {
        render(products);
        loading.classList.remove("is-visible");
      });
    });
  }

  window.toggleFilters = function () {
    document.getElementById("filter-panel").classList.toggle("is-open");
  };

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    status.textContent = "Thanks, offers will be sent to " + document.getElementById("email").value + ".";
  });

  load();
})();
