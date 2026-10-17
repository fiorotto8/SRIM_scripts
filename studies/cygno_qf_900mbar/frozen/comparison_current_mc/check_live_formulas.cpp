#include <cmath>
#include <iostream>
#include <iomanip>
using Double_t=double; using std::pow;
static const double k_helium=0.11700000000000001;
static const double a_helium=3.8999999999999999;
static const double b_helium=0.44;
Double_t f_helium(Double_t E) {
    return k_helium * (E + a_helium * pow(E, b_helium));
}
Double_t fprime_helium(Double_t E) {
    return k_helium * (1.0 + a_helium * b_helium * pow(E, b_helium - 1.0));
}
Double_t QF_helium(Double_t E) {
    Double_t val = f_helium(E);
    return val / (1.0 + val);
}
Double_t QFprime_helium(Double_t E) {
    Double_t fp = fprime_helium(E);
    Double_t fE = f_helium(E);
    return fp / pow(1.0 + fE, 2.0);
}
Double_t dRdE_helium(Double_t E) {
    return QF_helium(E) + E * QFprime_helium(E);
}
static const double k_carbon=0.0195;
static const double a_carbon=14.699999999999999;
static const double b_carbon=0.33000000000000002;
Double_t f_carbon(Double_t E) {
    return k_carbon * (E + a_carbon * pow(E, b_carbon));
}
Double_t fprime_carbon(Double_t E) {
    return k_carbon * (1.0 + a_carbon * b_carbon * pow(E, b_carbon - 1.0));
}
Double_t QF_carbon(Double_t E) {
    Double_t val = f_carbon(E);
    return val / (1.0 + val);
}
Double_t QFprime_carbon(Double_t E) {
    Double_t fp = fprime_carbon(E);
    Double_t fE = f_carbon(E);
    return fp / pow(1.0 + fE, 2.0);
}
Double_t dRdE_carbon(Double_t E) {
    return QF_carbon(E) + E * QFprime_carbon(E);
}
static const double k_flor=0.0083000000000000001;
static const double a_flor=27.399999999999999;
static const double b_flor=0.30299999999999999;
Double_t f_flor(Double_t E) {
    return k_flor * (E + a_flor * pow(E, b_flor));
}
Double_t fprime_flor(Double_t E) {
    return k_flor * (1.0 + a_flor * b_flor * pow(E, b_flor - 1.0));
}
Double_t QF_flor(Double_t E) {
    Double_t val = f_flor(E);
    return val / (1.0 + val);
}
Double_t QFprime_flor(Double_t E) {
    Double_t fp = fprime_flor(E);
    Double_t fE = f_flor(E);
    return fp / pow(1.0 + fE, 2.0);
}
Double_t dRdE_flor(Double_t E) {
    return QF_flor(E) + E * QFprime_flor(E);
}
int main(){std::cout<<std::setprecision(17);
std::cout<<"He4 0.25 "<<QF_helium(0.25)<<" "<<dRdE_helium(0.25)<<"\n";
std::cout<<"He4 0.30760366699322306 "<<QF_helium(0.30760366699322306)<<" "<<dRdE_helium(0.30760366699322306)<<"\n";
std::cout<<"He4 0.37848006379071081 "<<QF_helium(0.37848006379071081)<<" "<<dRdE_helium(0.37848006379071081)<<"\n";
std::cout<<"He4 0.46568742202340657 "<<QF_helium(0.46568742202340657)<<" "<<dRdE_helium(0.46568742202340657)<<"\n";
std::cout<<"He4 0.57298863474808193 "<<QF_helium(0.57298863474808193)<<" "<<dRdE_helium(0.57298863474808193)<<"\n";
std::cout<<"He4 0.70501362077580221 "<<QF_helium(0.70501362077580221)<<" "<<dRdE_helium(0.70501362077580221)<<"\n";
std::cout<<"He4 0.86745910012322558 "<<QF_helium(0.86745910012322558)<<" "<<dRdE_helium(0.86745910012322558)<<"\n";
std::cout<<"He4 1 "<<QF_helium(1)<<" "<<dRdE_helium(1)<<"\n";
std::cout<<"He4 1.0673344006581824 "<<QF_helium(1.0673344006581824)<<" "<<dRdE_helium(1.0673344006581824)<<"\n";
std::cout<<"He4 1.3132639022018837 "<<QF_helium(1.3132639022018837)<<" "<<dRdE_helium(1.3132639022018837)<<"\n";
std::cout<<"He4 1.6158591681885157 "<<QF_helium(1.6158591681885157)<<" "<<dRdE_helium(1.6158591681885157)<<"\n";
std::cout<<"He4 1.9881768219176268 "<<QF_helium(1.9881768219176268)<<" "<<dRdE_helium(1.9881768219176268)<<"\n";
std::cout<<"He4 2.4462819242111777 "<<QF_helium(2.4462819242111777)<<" "<<dRdE_helium(2.4462819242111777)<<"\n";
std::cout<<"He4 3.0099411615463851 "<<QF_helium(3.0099411615463851)<<" "<<dRdE_helium(3.0099411615463851)<<"\n";
std::cout<<"He4 3.7034757549020365 "<<QF_helium(3.7034757549020365)<<" "<<dRdE_helium(3.7034757549020365)<<"\n";
std::cout<<"He4 4.5568108913134475 "<<QF_helium(4.5568108913134475)<<" "<<dRdE_helium(4.5568108913134475)<<"\n";
std::cout<<"He4 5.6067669598506935 "<<QF_helium(5.6067669598506935)<<" "<<dRdE_helium(5.6067669598506935)<<"\n";
std::cout<<"He4 6.8986483073060763 "<<QF_helium(6.8986483073060763)<<" "<<dRdE_helium(6.8986483073060763)<<"\n";
std::cout<<"He4 8.4881980664957624 "<<QF_helium(8.4881980664957624)<<" "<<dRdE_helium(8.4881980664957624)<<"\n";
std::cout<<"He4 10 "<<QF_helium(10)<<" "<<dRdE_helium(10)<<"\n";
std::cout<<"He4 10.444003405675533 "<<QF_helium(10.444003405675533)<<" "<<dRdE_helium(10.444003405675533)<<"\n";
std::cout<<"He4 12.85045498270202 "<<QF_helium(12.85045498270202)<<" "<<dRdE_helium(12.85045498270202)<<"\n";
std::cout<<"He4 15.811388300841895 "<<QF_helium(15.811388300841895)<<" "<<dRdE_helium(15.811388300841895)<<"\n";
std::cout<<"He4 19.454564086370858 "<<QF_helium(19.454564086370858)<<" "<<dRdE_helium(19.454564086370858)<<"\n";
std::cout<<"He4 23.937181010889365 "<<QF_helium(23.937181010889365)<<" "<<dRdE_helium(23.937181010889365)<<"\n";
std::cout<<"He4 29.452658625720467 "<<QF_helium(29.452658625720467)<<" "<<dRdE_helium(29.452658625720467)<<"\n";
std::cout<<"He4 36.238983183884805 "<<QF_helium(36.238983183884805)<<" "<<dRdE_helium(36.238983183884805)<<"\n";
std::cout<<"He4 44.588976461874815 "<<QF_helium(44.588976461874815)<<" "<<dRdE_helium(44.588976461874815)<<"\n";
std::cout<<"He4 54.862930668588824 "<<QF_helium(54.862930668588824)<<" "<<dRdE_helium(54.862930668588824)<<"\n";
std::cout<<"He4 67.504154622611551 "<<QF_helium(67.504154622611551)<<" "<<dRdE_helium(67.504154622611551)<<"\n";
std::cout<<"He4 83.058101996771398 "<<QF_helium(83.058101996771398)<<" "<<dRdE_helium(83.058101996771398)<<"\n";
std::cout<<"He4 100 "<<QF_helium(100)<<" "<<dRdE_helium(100)<<"\n";
std::cout<<"He4 102.19590699081613 "<<QF_helium(102.19590699081613)<<" "<<dRdE_helium(102.19590699081613)<<"\n";
std::cout<<"He4 125.74334296829353 "<<QF_helium(125.74334296829353)<<" "<<dRdE_helium(125.74334296829353)<<"\n";
std::cout<<"He4 154.71645358813444 "<<QF_helium(154.71645358813444)<<" "<<dRdE_helium(154.71645358813444)<<"\n";
std::cout<<"He4 190.36539387158791 "<<QF_helium(190.36539387158791)<<" "<<dRdE_helium(190.36539387158791)<<"\n";
std::cout<<"He4 234.22837289403878 "<<QF_helium(234.22837289403878)<<" "<<dRdE_helium(234.22837289403878)<<"\n";
std::cout<<"He4 288.19802566424966 "<<QF_helium(288.19802566424966)<<" "<<dRdE_helium(288.19802566424966)<<"\n";
std::cout<<"He4 354.6030780581205 "<<QF_helium(354.6030780581205)<<" "<<dRdE_helium(354.6030780581205)<<"\n";
std::cout<<"He4 436.30882855104812 "<<QF_helium(436.30882855104812)<<" "<<dRdE_helium(436.30882855104812)<<"\n";
std::cout<<"He4 536.84078241527959 "<<QF_helium(536.84078241527959)<<" "<<dRdE_helium(536.84078241527959)<<"\n";
std::cout<<"He4 660.5367730498042 "<<QF_helium(660.5367730498042)<<" "<<dRdE_helium(660.5367730498042)<<"\n";
std::cout<<"He4 812.73413429596076 "<<QF_helium(812.73413429596076)<<" "<<dRdE_helium(812.73413429596076)<<"\n";
std::cout<<"He4 1000 "<<QF_helium(1000)<<" "<<dRdE_helium(1000)<<"\n";
std::cout<<"C12 0.25 "<<QF_carbon(0.25)<<" "<<dRdE_carbon(0.25)<<"\n";
std::cout<<"C12 0.30760366699322306 "<<QF_carbon(0.30760366699322306)<<" "<<dRdE_carbon(0.30760366699322306)<<"\n";
std::cout<<"C12 0.37848006379071081 "<<QF_carbon(0.37848006379071081)<<" "<<dRdE_carbon(0.37848006379071081)<<"\n";
std::cout<<"C12 0.46568742202340657 "<<QF_carbon(0.46568742202340657)<<" "<<dRdE_carbon(0.46568742202340657)<<"\n";
std::cout<<"C12 0.57298863474808193 "<<QF_carbon(0.57298863474808193)<<" "<<dRdE_carbon(0.57298863474808193)<<"\n";
std::cout<<"C12 0.70501362077580221 "<<QF_carbon(0.70501362077580221)<<" "<<dRdE_carbon(0.70501362077580221)<<"\n";
std::cout<<"C12 0.86745910012322558 "<<QF_carbon(0.86745910012322558)<<" "<<dRdE_carbon(0.86745910012322558)<<"\n";
std::cout<<"C12 1 "<<QF_carbon(1)<<" "<<dRdE_carbon(1)<<"\n";
std::cout<<"C12 1.0673344006581824 "<<QF_carbon(1.0673344006581824)<<" "<<dRdE_carbon(1.0673344006581824)<<"\n";
std::cout<<"C12 1.3132639022018837 "<<QF_carbon(1.3132639022018837)<<" "<<dRdE_carbon(1.3132639022018837)<<"\n";
std::cout<<"C12 1.6158591681885157 "<<QF_carbon(1.6158591681885157)<<" "<<dRdE_carbon(1.6158591681885157)<<"\n";
std::cout<<"C12 1.9881768219176268 "<<QF_carbon(1.9881768219176268)<<" "<<dRdE_carbon(1.9881768219176268)<<"\n";
std::cout<<"C12 2.4462819242111777 "<<QF_carbon(2.4462819242111777)<<" "<<dRdE_carbon(2.4462819242111777)<<"\n";
std::cout<<"C12 3.0099411615463851 "<<QF_carbon(3.0099411615463851)<<" "<<dRdE_carbon(3.0099411615463851)<<"\n";
std::cout<<"C12 3.7034757549020365 "<<QF_carbon(3.7034757549020365)<<" "<<dRdE_carbon(3.7034757549020365)<<"\n";
std::cout<<"C12 4.5568108913134475 "<<QF_carbon(4.5568108913134475)<<" "<<dRdE_carbon(4.5568108913134475)<<"\n";
std::cout<<"C12 5.6067669598506935 "<<QF_carbon(5.6067669598506935)<<" "<<dRdE_carbon(5.6067669598506935)<<"\n";
std::cout<<"C12 6.8986483073060763 "<<QF_carbon(6.8986483073060763)<<" "<<dRdE_carbon(6.8986483073060763)<<"\n";
std::cout<<"C12 8.4881980664957624 "<<QF_carbon(8.4881980664957624)<<" "<<dRdE_carbon(8.4881980664957624)<<"\n";
std::cout<<"C12 10 "<<QF_carbon(10)<<" "<<dRdE_carbon(10)<<"\n";
std::cout<<"C12 10.444003405675533 "<<QF_carbon(10.444003405675533)<<" "<<dRdE_carbon(10.444003405675533)<<"\n";
std::cout<<"C12 12.85045498270202 "<<QF_carbon(12.85045498270202)<<" "<<dRdE_carbon(12.85045498270202)<<"\n";
std::cout<<"C12 15.811388300841895 "<<QF_carbon(15.811388300841895)<<" "<<dRdE_carbon(15.811388300841895)<<"\n";
std::cout<<"C12 19.454564086370858 "<<QF_carbon(19.454564086370858)<<" "<<dRdE_carbon(19.454564086370858)<<"\n";
std::cout<<"C12 23.937181010889365 "<<QF_carbon(23.937181010889365)<<" "<<dRdE_carbon(23.937181010889365)<<"\n";
std::cout<<"C12 29.452658625720467 "<<QF_carbon(29.452658625720467)<<" "<<dRdE_carbon(29.452658625720467)<<"\n";
std::cout<<"C12 36.238983183884805 "<<QF_carbon(36.238983183884805)<<" "<<dRdE_carbon(36.238983183884805)<<"\n";
std::cout<<"C12 44.588976461874815 "<<QF_carbon(44.588976461874815)<<" "<<dRdE_carbon(44.588976461874815)<<"\n";
std::cout<<"C12 54.862930668588824 "<<QF_carbon(54.862930668588824)<<" "<<dRdE_carbon(54.862930668588824)<<"\n";
std::cout<<"C12 67.504154622611551 "<<QF_carbon(67.504154622611551)<<" "<<dRdE_carbon(67.504154622611551)<<"\n";
std::cout<<"C12 83.058101996771398 "<<QF_carbon(83.058101996771398)<<" "<<dRdE_carbon(83.058101996771398)<<"\n";
std::cout<<"C12 100 "<<QF_carbon(100)<<" "<<dRdE_carbon(100)<<"\n";
std::cout<<"C12 102.19590699081613 "<<QF_carbon(102.19590699081613)<<" "<<dRdE_carbon(102.19590699081613)<<"\n";
std::cout<<"C12 125.74334296829353 "<<QF_carbon(125.74334296829353)<<" "<<dRdE_carbon(125.74334296829353)<<"\n";
std::cout<<"C12 154.71645358813444 "<<QF_carbon(154.71645358813444)<<" "<<dRdE_carbon(154.71645358813444)<<"\n";
std::cout<<"C12 190.36539387158791 "<<QF_carbon(190.36539387158791)<<" "<<dRdE_carbon(190.36539387158791)<<"\n";
std::cout<<"C12 234.22837289403878 "<<QF_carbon(234.22837289403878)<<" "<<dRdE_carbon(234.22837289403878)<<"\n";
std::cout<<"C12 288.19802566424966 "<<QF_carbon(288.19802566424966)<<" "<<dRdE_carbon(288.19802566424966)<<"\n";
std::cout<<"C12 354.6030780581205 "<<QF_carbon(354.6030780581205)<<" "<<dRdE_carbon(354.6030780581205)<<"\n";
std::cout<<"C12 436.30882855104812 "<<QF_carbon(436.30882855104812)<<" "<<dRdE_carbon(436.30882855104812)<<"\n";
std::cout<<"C12 536.84078241527959 "<<QF_carbon(536.84078241527959)<<" "<<dRdE_carbon(536.84078241527959)<<"\n";
std::cout<<"C12 660.5367730498042 "<<QF_carbon(660.5367730498042)<<" "<<dRdE_carbon(660.5367730498042)<<"\n";
std::cout<<"C12 812.73413429596076 "<<QF_carbon(812.73413429596076)<<" "<<dRdE_carbon(812.73413429596076)<<"\n";
std::cout<<"C12 1000 "<<QF_carbon(1000)<<" "<<dRdE_carbon(1000)<<"\n";
std::cout<<"F19 0.25 "<<QF_flor(0.25)<<" "<<dRdE_flor(0.25)<<"\n";
std::cout<<"F19 0.30760366699322306 "<<QF_flor(0.30760366699322306)<<" "<<dRdE_flor(0.30760366699322306)<<"\n";
std::cout<<"F19 0.37848006379071081 "<<QF_flor(0.37848006379071081)<<" "<<dRdE_flor(0.37848006379071081)<<"\n";
std::cout<<"F19 0.46568742202340657 "<<QF_flor(0.46568742202340657)<<" "<<dRdE_flor(0.46568742202340657)<<"\n";
std::cout<<"F19 0.57298863474808193 "<<QF_flor(0.57298863474808193)<<" "<<dRdE_flor(0.57298863474808193)<<"\n";
std::cout<<"F19 0.70501362077580221 "<<QF_flor(0.70501362077580221)<<" "<<dRdE_flor(0.70501362077580221)<<"\n";
std::cout<<"F19 0.86745910012322558 "<<QF_flor(0.86745910012322558)<<" "<<dRdE_flor(0.86745910012322558)<<"\n";
std::cout<<"F19 1 "<<QF_flor(1)<<" "<<dRdE_flor(1)<<"\n";
std::cout<<"F19 1.0673344006581824 "<<QF_flor(1.0673344006581824)<<" "<<dRdE_flor(1.0673344006581824)<<"\n";
std::cout<<"F19 1.3132639022018837 "<<QF_flor(1.3132639022018837)<<" "<<dRdE_flor(1.3132639022018837)<<"\n";
std::cout<<"F19 1.6158591681885157 "<<QF_flor(1.6158591681885157)<<" "<<dRdE_flor(1.6158591681885157)<<"\n";
std::cout<<"F19 1.9881768219176268 "<<QF_flor(1.9881768219176268)<<" "<<dRdE_flor(1.9881768219176268)<<"\n";
std::cout<<"F19 2.4462819242111777 "<<QF_flor(2.4462819242111777)<<" "<<dRdE_flor(2.4462819242111777)<<"\n";
std::cout<<"F19 3.0099411615463851 "<<QF_flor(3.0099411615463851)<<" "<<dRdE_flor(3.0099411615463851)<<"\n";
std::cout<<"F19 3.7034757549020365 "<<QF_flor(3.7034757549020365)<<" "<<dRdE_flor(3.7034757549020365)<<"\n";
std::cout<<"F19 4.5568108913134475 "<<QF_flor(4.5568108913134475)<<" "<<dRdE_flor(4.5568108913134475)<<"\n";
std::cout<<"F19 5.6067669598506935 "<<QF_flor(5.6067669598506935)<<" "<<dRdE_flor(5.6067669598506935)<<"\n";
std::cout<<"F19 6.8986483073060763 "<<QF_flor(6.8986483073060763)<<" "<<dRdE_flor(6.8986483073060763)<<"\n";
std::cout<<"F19 8.4881980664957624 "<<QF_flor(8.4881980664957624)<<" "<<dRdE_flor(8.4881980664957624)<<"\n";
std::cout<<"F19 10 "<<QF_flor(10)<<" "<<dRdE_flor(10)<<"\n";
std::cout<<"F19 10.444003405675533 "<<QF_flor(10.444003405675533)<<" "<<dRdE_flor(10.444003405675533)<<"\n";
std::cout<<"F19 12.85045498270202 "<<QF_flor(12.85045498270202)<<" "<<dRdE_flor(12.85045498270202)<<"\n";
std::cout<<"F19 15.811388300841895 "<<QF_flor(15.811388300841895)<<" "<<dRdE_flor(15.811388300841895)<<"\n";
std::cout<<"F19 19.454564086370858 "<<QF_flor(19.454564086370858)<<" "<<dRdE_flor(19.454564086370858)<<"\n";
std::cout<<"F19 23.937181010889365 "<<QF_flor(23.937181010889365)<<" "<<dRdE_flor(23.937181010889365)<<"\n";
std::cout<<"F19 29.452658625720467 "<<QF_flor(29.452658625720467)<<" "<<dRdE_flor(29.452658625720467)<<"\n";
std::cout<<"F19 36.238983183884805 "<<QF_flor(36.238983183884805)<<" "<<dRdE_flor(36.238983183884805)<<"\n";
std::cout<<"F19 44.588976461874815 "<<QF_flor(44.588976461874815)<<" "<<dRdE_flor(44.588976461874815)<<"\n";
std::cout<<"F19 54.862930668588824 "<<QF_flor(54.862930668588824)<<" "<<dRdE_flor(54.862930668588824)<<"\n";
std::cout<<"F19 67.504154622611551 "<<QF_flor(67.504154622611551)<<" "<<dRdE_flor(67.504154622611551)<<"\n";
std::cout<<"F19 83.058101996771398 "<<QF_flor(83.058101996771398)<<" "<<dRdE_flor(83.058101996771398)<<"\n";
std::cout<<"F19 100 "<<QF_flor(100)<<" "<<dRdE_flor(100)<<"\n";
std::cout<<"F19 102.19590699081613 "<<QF_flor(102.19590699081613)<<" "<<dRdE_flor(102.19590699081613)<<"\n";
std::cout<<"F19 125.74334296829353 "<<QF_flor(125.74334296829353)<<" "<<dRdE_flor(125.74334296829353)<<"\n";
std::cout<<"F19 154.71645358813444 "<<QF_flor(154.71645358813444)<<" "<<dRdE_flor(154.71645358813444)<<"\n";
std::cout<<"F19 190.36539387158791 "<<QF_flor(190.36539387158791)<<" "<<dRdE_flor(190.36539387158791)<<"\n";
std::cout<<"F19 234.22837289403878 "<<QF_flor(234.22837289403878)<<" "<<dRdE_flor(234.22837289403878)<<"\n";
std::cout<<"F19 288.19802566424966 "<<QF_flor(288.19802566424966)<<" "<<dRdE_flor(288.19802566424966)<<"\n";
std::cout<<"F19 354.6030780581205 "<<QF_flor(354.6030780581205)<<" "<<dRdE_flor(354.6030780581205)<<"\n";
std::cout<<"F19 436.30882855104812 "<<QF_flor(436.30882855104812)<<" "<<dRdE_flor(436.30882855104812)<<"\n";
std::cout<<"F19 536.84078241527959 "<<QF_flor(536.84078241527959)<<" "<<dRdE_flor(536.84078241527959)<<"\n";
std::cout<<"F19 660.5367730498042 "<<QF_flor(660.5367730498042)<<" "<<dRdE_flor(660.5367730498042)<<"\n";
std::cout<<"F19 812.73413429596076 "<<QF_flor(812.73413429596076)<<" "<<dRdE_flor(812.73413429596076)<<"\n";
std::cout<<"F19 1000 "<<QF_flor(1000)<<" "<<dRdE_flor(1000)<<"\n";
}
