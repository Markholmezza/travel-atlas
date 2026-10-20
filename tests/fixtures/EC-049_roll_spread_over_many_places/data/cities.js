/* Test fixture: a minimal, valid copy of data/cities.js (four places, three countries). */
const CITIES = [
  {id:"berlin",  en:"Berlin",  country:"Germany", region:"Europe", lat:52.5200, lng:13.4050, hue:265, size:"big",
   photos:["01.jpg",{file:"02.jpg", film:true, roll:"Portra400-01", scan:"000001"}]},
  {id:"potsdam", en:"Potsdam", country:"Germany", region:"Europe", lat:52.3906, lng:13.0645, hue:120, size:"small", parent:"berlin",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000002"}]},
  {id:"atlanta", en:"Atlanta", country:"United States", state:"Georgia", region:"North America", lat:33.7490, lng:-84.3880, hue:95, size:"big",
   photos:["01.jpg"]},
  {id:"zurich",  en:"Zurich", local:"Zürich", country:"Switzerland", canton:"Zürich", region:"Europe", lat:47.3769, lng:8.5417, hue:190, size:"big",
   photos:["01.jpg"]},
  {id:"hamburg", en:"Hamburg", country:"Germany", region:"Europe", lat:53.5511, lng:9.9937, hue:30, size:"big",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000010"}]},
  {id:"munich", en:"Munich", country:"Germany", region:"Europe", lat:48.1372, lng:11.5755, hue:30, size:"big",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000011"}]},
  {id:"frankfurt", en:"Frankfurt", country:"Germany", region:"Europe", lat:50.1109, lng:8.6821, hue:30, size:"big",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000012"}]},
  {id:"cologne", en:"Cologne", country:"Germany", region:"Europe", lat:50.9375, lng:6.9603, hue:30, size:"big",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000013"}]},
  {id:"geneva", en:"Geneva", country:"Switzerland", region:"Europe", lat:46.2044, lng:6.1432, hue:30, size:"big",
   photos:[{file:"01.jpg", film:true, roll:"Portra400-01", scan:"000014"}]},
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
