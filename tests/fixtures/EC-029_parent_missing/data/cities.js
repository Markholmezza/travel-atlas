/* Test fixture: a minimal, valid copy of data/cities.js (four places, three countries). */
const CITIES = [
  {id:"berlin",  en:"Berlin",  country:"Germany", region:"Europe", lat:52.5200, lng:13.4050, hue:265, size:"big",
   photos:["01.jpg",{file:"02.jpg", film:true, roll:"Portra400-01", scan:"000001"}]},
  {id:"potsdam", en:"Potsdam", country:"Germany", region:"Europe", lat:52.3906, lng:13.0645, hue:120, size:"small", parent:"brandenburg",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000002"}]},
  {id:"atlanta", en:"Atlanta", country:"United States", state:"Georgia", region:"North America", lat:33.7490, lng:-84.3880, hue:95, size:"big",
   photos:["01.jpg"]},
  {id:"zurich",  en:"Zurich", local:"Zürich", country:"Switzerland", canton:"Zürich", region:"Europe", lat:47.3769, lng:8.5417, hue:190, size:"big",
   photos:["01.jpg"]},
];

const COUNTRIES = {
  "Germany":{iso2:"de",iso:"276"}, "United States":{iso2:"us",iso:"840"}, "Switzerland":{iso2:"ch",iso:"756"},
};
const REGIONS = [
  {name:"North America", slug:"north-america"},
  {name:"Europe",        slug:"europe"},
];
const US_STATES = {
  "California":{code:"CA"}, "Georgia":{code:"GA"},
};
const SWISS_CANTONS = {
  "Zürich":{code:"ZH",en:"Zurich"},
};
